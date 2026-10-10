#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <limits.h>
#include <stdint.h>
#include <linux/landlock.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>

#ifndef LANDLOCK_ACCESS_FS_REFER
#define LANDLOCK_ACCESS_FS_REFER (1ULL << 13)
#endif
#ifndef LANDLOCK_ACCESS_FS_TRUNCATE
#define LANDLOCK_ACCESS_FS_TRUNCATE (1ULL << 14)
#endif
#ifndef LANDLOCK_ACCESS_FS_IOCTL_DEV
#define LANDLOCK_ACCESS_FS_IOCTL_DEV (1ULL << 15)
#endif
#ifndef LANDLOCK_CREATE_RULESET_VERSION
#define LANDLOCK_CREATE_RULESET_VERSION (1U << 0)
#endif
#ifndef LANDLOCK_RULE_PATH_BENEATH
#define LANDLOCK_RULE_PATH_BENEATH 1
#endif
#ifndef LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET
#define LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET (1ULL << 0)
#endif
#ifndef LANDLOCK_SCOPE_SIGNAL
#define LANDLOCK_SCOPE_SIGNAL (1ULL << 1)
#endif

/* ABI 6 is the floor. The size is exactly these three words so an older
 * header cannot drop `scoped`. There is no fallback ruleset. */
struct launch_ruleset_attr {
    uint64_t handled_access_fs;
    uint64_t handled_access_net;
    uint64_t scoped;
};

#define MIN_ABI 6
#define RULESET_ATTR_SIZE sizeof(struct launch_ruleset_attr)
#define REQUIRED_SCOPE (LANDLOCK_SCOPE_SIGNAL | LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET)
#define CAP_SETGID 6
#define CAP_SETUID 7
#define CAP_SETPCAP 8
#define CAP_VERSION_3 0x20080522
#define WORKER_GID 10001

struct cap_header {
    uint32_t version;
    int pid;
};
struct cap_data {
    uint32_t effective;
    uint32_t permitted;
    uint32_t inheritable;
};

static int landlock_abi(void) {
    return (int)syscall(__NR_landlock_create_ruleset, NULL, 0, LANDLOCK_CREATE_RULESET_VERSION);
}

static uint64_t handled_fs(int abi) {
    uint64_t mask = LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_WRITE_FILE |
                    LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR |
                    LANDLOCK_ACCESS_FS_REMOVE_DIR | LANDLOCK_ACCESS_FS_REMOVE_FILE |
                    LANDLOCK_ACCESS_FS_MAKE_CHAR | LANDLOCK_ACCESS_FS_MAKE_DIR |
                    LANDLOCK_ACCESS_FS_MAKE_REG | LANDLOCK_ACCESS_FS_MAKE_SOCK |
                    LANDLOCK_ACCESS_FS_MAKE_FIFO | LANDLOCK_ACCESS_FS_MAKE_BLOCK |
                    LANDLOCK_ACCESS_FS_MAKE_SYM | LANDLOCK_ACCESS_FS_REFER |
                    LANDLOCK_ACCESS_FS_TRUNCATE;
    if (abi >= 5) {
        mask |= LANDLOCK_ACCESS_FS_IOCTL_DEV;
    }
    return mask;
}

static int add_path(int ruleset, const char *path, uint64_t rights) {
    int fd = open(path, O_PATH | O_CLOEXEC);
    if (fd < 0) {
        return -1;
    }
    struct landlock_path_beneath_attr attr = {
        .allowed_access = rights,
        .parent_fd = fd,
    };
    int rc = (int)syscall(__NR_landlock_add_rule, ruleset, LANDLOCK_RULE_PATH_BENEATH, &attr, 0);
    int saved = errno;
    close(fd);
    errno = saved;
    return rc;
}

static int covers(const char *root, const char *child) {
    char rbuf[PATH_MAX];
    char cbuf[PATH_MAX];
    const char *r = root;
    const char *c = child;
    if (realpath(root, rbuf) != NULL) {
        r = rbuf;
    }
    if (realpath(child, cbuf) != NULL) {
        c = cbuf;
    }
    if (strcmp(r, "/") == 0) {
        return 1;
    }
    size_t n = strlen(r);
    while (n > 1 && r[n - 1] == '/') {
        n--;
    }
    if (strncmp(c, r, n) != 0) {
        return 0;
    }
    return c[n] == '\0' || c[n] == '/';
}

static void close_extra_fds(void) {
    int dirfd = open("/proc/self/fd", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (dirfd < 0) {
        perror("fd inventory");
        _exit(85);
    }
    DIR *dir = fdopendir(dirfd);
    if (!dir) {
        close(dirfd);
        _exit(85);
    }
    int keep[64];
    int nkeep = 0;
    struct dirent *ent;
    while ((ent = readdir(dir)) != NULL) {
        if (ent->d_name[0] == '.') {
            continue;
        }
        int fd = atoi(ent->d_name);
        if (fd == dirfd || fd <= STDERR_FILENO) {
            continue;
        }
        if (nkeep < 64) {
            keep[nkeep++] = fd;
        } else {
            close(fd);
        }
    }
    closedir(dir);
    for (int i = 0; i < nkeep; i++) {
        close(keep[i]);
    }
}

static void capset_three(void) {
    struct cap_header hdr = {.version = CAP_VERSION_3, .pid = 0};
    struct cap_data data[2];
    memset(data, 0, sizeof(data));
    uint32_t low = (1U << CAP_SETGID) | (1U << CAP_SETUID) | (1U << CAP_SETPCAP);
    data[0].effective = low;
    data[0].permitted = low;
    if (syscall(__NR_capset, &hdr, data) != 0) {
        perror("capset");
        _exit(80);
    }
    if (prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0) != 0) {
        _exit(80);
    }
}

static void drop_bounding_set(void) {
    for (int cap = 0; cap < 64; cap++) {
        errno = 0;
        int present = prctl(PR_CAPBSET_READ, cap, 0, 0, 0);
        if (present < 0 && errno == EINVAL) {
            break;
        }
        if (present < 0 || prctl(PR_CAPBSET_DROP, cap, 0, 0, 0) != 0 ||
            prctl(PR_CAPBSET_READ, cap, 0, 0, 0) != 0) {
            _exit(80);
        }
    }
}

static void become_tool(void) {
    if (setgroups(0, NULL) != 0) {
        perror("setgroups");
        _exit(81);
    }
    if (setresgid(10003, 10003, 10003) != 0) {
        perror("setresgid");
        _exit(82);
    }
    if (setresuid(10003, 10003, 10003) != 0) {
        perror("setresuid");
        _exit(83);
    }
    struct cap_header hdr = {.version = CAP_VERSION_3, .pid = 0};
    struct cap_data data[2];
    memset(data, 0, sizeof(data));
    if (syscall(__NR_capset, &hdr, data) != 0 ||
        syscall(__NR_capget, &hdr, data) != 0 ||
        data[0].effective || data[0].permitted || data[0].inheritable ||
        data[1].effective || data[1].permitted || data[1].inheritable ||
        prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1) {
        _exit(80);
    }
}

static int add_ro_file(int ruleset, const char *path) {
    if (access(path, F_OK) != 0) {
        return 0;
    }
    if (add_path(ruleset, path, LANDLOCK_ACCESS_FS_READ_FILE) != 0) {
        perror(path);
        return -1;
    }
    return 0;
}

static int add_ro_dir(int ruleset, const char *path) {
    if (access(path, F_OK) != 0) {
        return 0;
    }
    if (add_path(ruleset, path,
                 LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR) != 0) {
        perror(path);
        return -1;
    }
    return 0;
}

static int apply_domain(int abi, const char *attempt, const char **ro, int nro,
                        const char **prot, int nprot, int allow_net_resolver) {
    if (abi < MIN_ABI) {
        fprintf(stderr, "landlock abi %d below required %d\n", abi, MIN_ABI);
        return -1;
    }
    for (int i = 0; i < nro; i++) {
        for (int j = 0; j < nprot; j++) {
            if (covers(ro[i], prot[j])) {
                fprintf(stderr, "refuse: ro root covers protected path\n");
                return -2;
            }
        }
    }
    struct launch_ruleset_attr attr;
    memset(&attr, 0, sizeof(attr));
    attr.handled_access_fs = handled_fs(abi);
    attr.handled_access_net = 0;
    attr.scoped = REQUIRED_SCOPE;
    int ruleset = (int)syscall(__NR_landlock_create_ruleset, &attr, RULESET_ATTR_SIZE, 0);
    if (ruleset < 0) {
        fprintf(stderr, "landlock_create_ruleset scoped errno %d (no fallback)\n", errno);
        return -1;
    }
    uint64_t work = LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_WRITE_FILE |
                    LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR |
                    LANDLOCK_ACCESS_FS_REMOVE_DIR | LANDLOCK_ACCESS_FS_REMOVE_FILE |
                    LANDLOCK_ACCESS_FS_MAKE_REG | LANDLOCK_ACCESS_FS_MAKE_DIR |
                    LANDLOCK_ACCESS_FS_TRUNCATE | LANDLOCK_ACCESS_FS_REFER;
    uint64_t ro_rights = LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE |
                         LANDLOCK_ACCESS_FS_READ_DIR;
    if (add_path(ruleset, attempt, work) != 0) {
        perror(attempt);
        close(ruleset);
        return -1;
    }
    for (int i = 0; i < nro; i++) {
        if (add_path(ruleset, ro[i], ro_rights) != 0) {
            perror(ro[i]);
            close(ruleset);
            return -1;
        }
    }
    uint64_t null_rights = LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_WRITE_FILE;
    uint64_t urandom_rights = LANDLOCK_ACCESS_FS_READ_FILE;
    if (abi >= 5) {
        null_rights |= LANDLOCK_ACCESS_FS_IOCTL_DEV;
        urandom_rights |= LANDLOCK_ACCESS_FS_IOCTL_DEV;
    }
    if (access("/dev/null", F_OK) == 0 && add_path(ruleset, "/dev/null", null_rights) != 0) {
        perror("/dev/null");
        close(ruleset);
        return -1;
    }
    if (access("/dev/urandom", F_OK) == 0 && add_path(ruleset, "/dev/urandom", urandom_rights) != 0) {
        perror("/dev/urandom");
        close(ruleset);
        return -1;
    }
    if (access("/etc/ld.so.cache", F_OK) == 0 &&
        add_path(ruleset, "/etc/ld.so.cache", LANDLOCK_ACCESS_FS_READ_FILE) != 0) {
        perror("/etc/ld.so.cache");
        close(ruleset);
        return -1;
    }
    if (allow_net_resolver) {
        /* Network profile only: glibc resolver + OpenSSL trust store. */
        if (add_ro_file(ruleset, "/etc/resolv.conf") != 0 ||
            add_ro_file(ruleset, "/etc/nsswitch.conf") != 0 ||
            add_ro_file(ruleset, "/etc/hosts") != 0 ||
            add_ro_file(ruleset, "/etc/protocols") != 0 ||
            add_ro_file(ruleset, "/etc/services") != 0 ||
            add_ro_file(ruleset, "/etc/ssl/openssl.cnf") != 0 ||
            add_ro_file(ruleset, "/etc/ssl/certs/ca-certificates.crt") != 0 ||
            add_ro_dir(ruleset, "/etc/ssl/certs") != 0) {
            close(ruleset);
            return -1;
        }
    }
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) {
        perror("PR_SET_NO_NEW_PRIVS");
        close(ruleset);
        return -1;
    }
    if (syscall(__NR_landlock_restrict_self, ruleset, 0) != 0) {
        fprintf(stderr, "landlock_restrict_self errno %d (no fallback)\n", errno);
        close(ruleset);
        return -1;
    }
    close(ruleset);
    return 0;
}

static int cmd_identity(void) {
    int abi = landlock_abi();
    printf("{\"abi\":%d,\"min_abi\":%d,\"scoped_mask\":%llu,\"ruleset_size\":%zu,"
           "\"handled_access_net\":0}\n",
           abi, MIN_ABI, (unsigned long long)REQUIRED_SCOPE, RULESET_ATTR_SIZE);
    return abi >= MIN_ABI ? 0 : 90;
}

static int cmd_launch(int argc, char **argv) {
    const char *attempt = NULL;
    const char *cgroup_procs = NULL;
    const char *ro[16];
    const char *prot[12];
    int nro = 0;
    int nprot = 0;
    int allow_net_resolver = 0;
    int argi = 2;
    while (argi < argc) {
        if (strcmp(argv[argi], "--") == 0) {
            argi++;
            break;
        }
        if (strcmp(argv[argi], "--attempt") == 0 && argi + 1 < argc) {
            attempt = argv[++argi];
        } else if (strcmp(argv[argi], "--cgroup-procs") == 0 && argi + 1 < argc) {
            cgroup_procs = argv[++argi];
        } else if (strcmp(argv[argi], "--allow-net-resolver") == 0) {
            allow_net_resolver = 1;
        } else if (strcmp(argv[argi], "--ro") == 0 && argi + 1 < argc && nro < 16) {
            ro[nro++] = argv[++argi];
        } else if (strcmp(argv[argi], "--protect") == 0 && argi + 1 < argc && nprot < 12) {
            prot[nprot++] = argv[++argi];
        } else {
            fprintf(stderr, "refuse exec: bad launcher argument\n");
            return 2;
        }
        argi++;
    }
    if (!attempt || argi >= argc) {
        fprintf(stderr, "refuse exec: missing attempt or argv\n");
        return 2;
    }
    int abi = landlock_abi();
    if (abi < MIN_ABI) {
        fprintf(stderr, "refuse exec: abi %d required %d\n", abi, MIN_ABI);
        return 90;
    }
    close_extra_fds();
    if (cgroup_procs) {
        char pidbuf[32];
        int n = snprintf(pidbuf, sizeof(pidbuf), "%d\n", (int)getpid());
        int fd = open(cgroup_procs, O_WRONLY | O_CLOEXEC);
        if (fd < 0 || write(fd, pidbuf, (size_t)n) != n) {
            perror(cgroup_procs);
            if (fd >= 0) {
                close(fd);
            }
            return 88;
        }
        close(fd);
    }
    capset_three();
    /* uid 0 has no DAC_OVERRIDE. Group 10001 can open the job directory. */
    if (setresgid(WORKER_GID, WORKER_GID, 0) != 0) {
        perror("setresgid worker");
        return 82;
    }
    int applied = apply_domain(abi, attempt, ro, nro, prot, nprot, allow_net_resolver);
    if (applied == -2) {
        return 93;
    }
    if (applied != 0) {
        return 92;
    }
    drop_bounding_set();
    umask(027);
    become_tool();
    char *envp[] = {"PATH=/usr/bin:/bin", "LANG=C", "LC_ALL=C", NULL};
    execve(argv[argi], &argv[argi], envp);
    perror("execve");
    return 84;
}

int main(int argc, char **argv) {
    if (argc >= 2 && strcmp(argv[1], "identity") == 0) {
        return cmd_identity();
    }
    if (argc >= 2 && strcmp(argv[1], "launch") == 0) {
        return cmd_launch(argc, argv);
    }
    fprintf(stderr, "usage: sec08-launch identity|launch\n");
    return 2;
}
