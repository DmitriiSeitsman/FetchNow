#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <limits.h>
#include <linux/landlock.h>
#include <stddef.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
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

/* ABI 6 is the floor: LANDLOCK_SCOPE_SIGNAL and abstract-unix scope live in
 * the third ruleset word. The size passed to the kernel is exactly these
 * three words so older headers cannot drop `scoped`. */
struct sec08_ruleset_attr {
    uint64_t handled_access_fs;
    uint64_t handled_access_net;
    uint64_t scoped;
};

#define MIN_ABI 6
#define RULESET_ATTR_SIZE sizeof(struct sec08_ruleset_attr)
#define REQUIRED_SCOPE (LANDLOCK_SCOPE_SIGNAL | LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET)
#define CAP_SETGID 6
#define CAP_SETUID 7
#define CAP_SETPCAP 8
#define CAP_VERSION_3 0x20080522
#define CONNECT_MS 1000

struct cap_header {
    uint32_t version;
    int pid;
};
struct cap_data {
    uint32_t effective;
    uint32_t permitted;
    uint32_t inheritable;
};

static int g_abi = -1;

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
        return;
    }
    DIR *dir = fdopendir(dirfd);
    if (!dir) {
        close(dirfd);
        return;
    }
    int keep[64];
    int nkeep = 0;
    struct dirent *ent;
    while ((ent = readdir(dir)) != NULL) {
        if (ent->d_name[0] == '.') {
            continue;
        }
        int fd = atoi(ent->d_name);
        if (fd == dirfd || fd == STDIN_FILENO || fd == STDOUT_FILENO || fd == STDERR_FILENO) {
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
    prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0);
}

static void drop_bounding_set(void) {
    for (int cap = 0; cap < 64; cap++) {
        if (cap == CAP_SETGID || cap == CAP_SETUID || cap == CAP_SETPCAP) {
            continue;
        }
        prctl(PR_CAPBSET_DROP, cap, 0, 0, 0);
    }
    prctl(PR_CAPBSET_DROP, CAP_SETPCAP, 0, 0, 0);
    prctl(PR_CAPBSET_DROP, CAP_SETUID, 0, 0, 0);
    prctl(PR_CAPBSET_DROP, CAP_SETGID, 0, 0, 0);
}

static void write_proc_status(const char *path) {
    FILE *in = fopen("/proc/self/status", "r");
    FILE *out = fopen(path, "w");
    if (!out) {
        if (in) {
            fclose(in);
        }
        return;
    }
    uid_t r, e, s;
    gid_t gr, ge, gs;
    getresuid(&r, &e, &s);
    getresgid(&gr, &ge, &gs);
    fprintf(out, "resuid %u %u %u\nresgid %u %u %u\nnnp %d\nseccomp %d\n", (unsigned)r, (unsigned)e,
            (unsigned)s, (unsigned)gr, (unsigned)ge, (unsigned)gs,
            prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0), prctl(PR_GET_SECCOMP, 0, 0, 0, 0));
    if (in) {
        char line[256];
        while (fgets(line, sizeof(line), in)) {
            if (strncmp(line, "Cap", 3) == 0 || strncmp(line, "NoNewPrivs", 10) == 0 ||
                strncmp(line, "Seccomp", 7) == 0 || strncmp(line, "Uid", 3) == 0 ||
                strncmp(line, "Gid", 3) == 0) {
                fputs(line, out);
            }
        }
        fclose(in);
    }
    fclose(out);
}

static int apply_domain(const char *attempt, const char **ro, int nro, const char **prot, int nprot,
                        const char *control_dir) {
    if (g_abi < MIN_ABI) {
        fprintf(stderr, "landlock abi %d below required %d\n", g_abi, MIN_ABI);
        return -1;
    }
    for (int i = 0; i < nro; i++) {
        for (int j = 0; j < nprot; j++) {
            if (covers(ro[i], prot[j])) {
                fprintf(stderr, "refuse: ro root %s covers protected %s\n", ro[i], prot[j]);
                return -2;
            }
        }
    }
    struct sec08_ruleset_attr attr;
    memset(&attr, 0, sizeof(attr));
    attr.handled_access_fs = handled_fs(g_abi);
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
        if (access(ro[i], F_OK) != 0) {
            fprintf(stderr, "ro root missing %s\n", ro[i]);
            close(ruleset);
            return -1;
        }
        if (add_path(ruleset, ro[i], ro_rights) != 0) {
            perror(ro[i]);
            close(ruleset);
            return -1;
        }
    }
    if (control_dir) {
        uint64_t ctl = LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_WRITE_FILE |
                       LANDLOCK_ACCESS_FS_READ_DIR;
        if (add_path(ruleset, control_dir, ctl) != 0) {
            perror(control_dir);
            close(ruleset);
            return -1;
        }
        fprintf(stderr, "allowed_control_dir %s rights=read,write,readdir layer=dac\n", control_dir);
    }
    uint64_t null_rights = LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_WRITE_FILE;
    uint64_t urandom_rights = LANDLOCK_ACCESS_FS_READ_FILE;
    if (g_abi >= 5) {
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
    fprintf(stderr,
            "ruleset abi=%d size=%zu scoped_mask=%llu net_handled=0 fs_mask=0x%llx\n",
            g_abi, RULESET_ATTR_SIZE, (unsigned long long)attr.scoped,
            (unsigned long long)attr.handled_access_fs);
    return 0;
}

static void become_tool(uid_t uid, gid_t gid) {
    if (setgroups(0, NULL) != 0) {
        perror("setgroups");
        _exit(81);
    }
    if (setresgid(gid, gid, gid) != 0) {
        perror("setresgid");
        _exit(82);
    }
    if (setresuid(uid, uid, uid) != 0) {
        perror("setresuid");
        _exit(83);
    }
}

static void exec_clean(const char *binary, char **argv) {
    char *envp[] = {"PATH=/usr/bin:/bin", "LANG=C", NULL};
    execve(binary, argv, envp);
    perror("execve");
    _exit(84);
}

static int cmd_cancel(int argc, char **argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: cancel KILL_PATH STATUS_PATH\n");
        return 2;
    }
    write_proc_status(argv[3]);
    int fd = open(argv[2], O_WRONLY | O_CLOEXEC);
    if (fd < 0) {
        perror(argv[2]);
        return 86;
    }
    if (write(fd, "1\n", 2) != 2) {
        perror("cgroup.kill");
        close(fd);
        return 87;
    }
    close(fd);
    return 0;
}

static int cmd_identity(void) {
    int abi = landlock_abi();
    printf("{\"abi\":%d,\"min_abi\":%d,\"scoped_mask\":%llu,\"ruleset_size\":%zu,"
           "\"handled_access_net\":0,\"euid\":%u}\n",
           abi, MIN_ABI, (unsigned long long)REQUIRED_SCOPE, RULESET_ATTR_SIZE, (unsigned)geteuid());
    return abi >= MIN_ABI ? 0 : 90;
}

struct launch_opts {
    const char *attempt;
    const char *binary;
    const char *status_path;
    const char *cgroup_procs;
    const char *control_dir;
    const char *ro[8];
    int nro;
    const char *prot[12];
    int nprot;
    uid_t tool_uid;
    gid_t tool_gid;
    int force_abi;
    int force_ruleset;
    char **exec_argv;
};

static int cmd_launch(struct launch_opts *opt) {
    g_abi = landlock_abi();
    fprintf(stderr, "entrypoint euid=%u abi=%d min=%d\n", (unsigned)geteuid(), g_abi, MIN_ABI);
    if (opt->force_abi || g_abi < MIN_ABI) {
        fprintf(stderr, "refuse exec: abi %d required %d force=%d\n", g_abi, MIN_ABI, opt->force_abi);
        return 90;
    }
    close_extra_fds();
    if (opt->cgroup_procs) {
        char pidbuf[32];
        int n = snprintf(pidbuf, sizeof(pidbuf), "%d\n", (int)getpid());
        int fd = open(opt->cgroup_procs, O_WRONLY | O_CLOEXEC);
        if (fd < 0 || write(fd, pidbuf, (size_t)n) != n) {
            perror(opt->cgroup_procs);
            if (fd >= 0) {
                close(fd);
            }
            return 88;
        }
        close(fd);
    }
    capset_three();
    if (opt->status_path) {
        write_proc_status(opt->status_path);
        if (opt->cgroup_procs) {
            FILE *cg = fopen("/proc/self/cgroup", "r");
            FILE *st = fopen(opt->status_path, "a");
            if (cg && st) {
                char line[256];
                fputs("cgroup ", st);
                while (fgets(line, sizeof(line), cg)) {
                    fputs(line, st);
                }
            }
            if (cg) {
                fclose(cg);
            }
            if (st) {
                fclose(st);
            }
        }
    }
    if (opt->force_ruleset) {
        if (syscall(__NR_landlock_restrict_self, -1, 0) == 0) {
            fprintf(stderr, "unexpected restrict success\n");
        }
        fprintf(stderr, "refuse exec: forced ruleset failure\n");
        return 91;
    }
    int applied = apply_domain(opt->attempt, opt->ro, opt->nro, opt->prot, opt->nprot, opt->control_dir);
    if (applied == -2) {
        fprintf(stderr, "refuse exec: ro root covers a protected path\n");
        return 93;
    }
    if (applied != 0) {
        fprintf(stderr, "refuse exec: domain failed\n");
        return 92;
    }
    drop_bounding_set();
    become_tool(opt->tool_uid, opt->tool_gid);
    exec_clean(opt->binary, opt->exec_argv);
    return 84;
}

static void json_escape(FILE *out, const char *s) {
    for (; s && *s; s++) {
        if (*s == '"' || *s == '\\') {
            fputc('\\', out);
        } else if (*s == '\n' || *s == '\r') {
            fputc(' ', out);
            continue;
        }
        fputc(*s, out);
    }
}

static void check(FILE *out, int *first, const char *name, int pass, const char *detail) {
    if (!*first) {
        fputc(',', out);
    }
    *first = 0;
    fprintf(out, "{\"name\":\"");
    json_escape(out, name);
    fprintf(out, "\",\"pass\":%s,\"detail\":\"", pass ? "true" : "false");
    json_escape(out, detail ? detail : "");
    fprintf(out, "\"}");
    fprintf(stderr, "CHECK %s %s %s\n", name, pass ? "PASS" : "FAIL", detail ? detail : "");
}

static const char *err_class(int ok, int err) {
    if (ok) {
        return "success";
    }
    if (err == EACCES || err == EPERM) {
        return "permission";
    }
    if (err == ETIMEDOUT || err == EAGAIN || err == EWOULDBLOCK || err == EINPROGRESS) {
        return "timeout";
    }
    return "other";
}

static int is_perm(int err) { return err == EACCES || err == EPERM; }

static int try_open_read(const char *path, int *err_out) {
    errno = 0;
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        if (err_out) {
            *err_out = errno;
        }
        return -1;
    }
    char buf[8];
    ssize_t n = read(fd, buf, sizeof(buf));
    int saved = errno;
    close(fd);
    if (n < 0 && err_out) {
        *err_out = saved;
        return -1;
    }
    if (err_out) {
        *err_out = 0;
    }
    return 0;
}

static int connect_timeout(int fd, const struct sockaddr *addr, socklen_t len, int ms, int *err_out) {
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) != 0) {
        *err_out = errno;
        return -1;
    }
    int rc = connect(fd, addr, len);
    if (rc == 0) {
        *err_out = 0;
        return 0;
    }
    if (errno != EINPROGRESS) {
        *err_out = errno;
        return -1;
    }
    struct pollfd pfd = {.fd = fd, .events = POLLOUT};
    int pr = poll(&pfd, 1, ms);
    if (pr == 0) {
        *err_out = ETIMEDOUT;
        return -1;
    }
    if (pr < 0) {
        *err_out = errno;
        return -1;
    }
    int soerr = 0;
    socklen_t sl = sizeof(soerr);
    if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &sl) != 0) {
        *err_out = errno;
        return -1;
    }
    if (soerr == 0) {
        *err_out = 0;
        return 0;
    }
    *err_out = soerr;
    return -1;
}

static int connect_unix_path(const char *path, int *err_out) {
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        *err_out = errno;
        return -1;
    }
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", path);
    int rc = connect_timeout(fd, (struct sockaddr *)&addr, sizeof(addr), CONNECT_MS, err_out);
    close(fd);
    return rc;
}

static int connect_abstract(const char *name, int *err_out) {
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        *err_out = errno;
        return -1;
    }
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    size_t n = strlen(name);
    if (n == 0 || n >= sizeof(addr.sun_path) - 1) {
        *err_out = EINVAL;
        close(fd);
        return -1;
    }
    addr.sun_path[0] = '\0';
    memcpy(addr.sun_path + 1, name, n);
    socklen_t len = (socklen_t)(offsetof(struct sockaddr_un, sun_path) + 1 + n);
    int rc = connect_timeout(fd, (struct sockaddr *)&addr, len, CONNECT_MS, err_out);
    close(fd);
    return rc;
}

static void cred_detail(char *buf, size_t n) {
    uid_t r, e, s;
    gid_t gr, ge, gs;
    getresuid(&r, &e, &s);
    getresgid(&gr, &ge, &gs);
    struct cap_header hdr = {.version = CAP_VERSION_3, .pid = 0};
    struct cap_data data[2];
    memset(data, 0, sizeof(data));
    syscall(__NR_capget, &hdr, data);
    int groups = getgroups(0, NULL);
    int bound = 0;
    for (int cap = 0; cap < 64; cap++) {
        if (prctl(PR_CAPBSET_READ, cap, 0, 0, 0) == 1) {
            bound++;
        }
    }
    snprintf(buf, n, "uid %u %u %u gid %u %u %u groups %d cap %08x/%08x/%08x bound %d nnp %d",
             (unsigned)r, (unsigned)e, (unsigned)s, (unsigned)gr, (unsigned)ge, (unsigned)gs, groups,
             data[0].effective, data[0].permitted, data[0].inheritable, bound,
             prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0));
}

static int wait_deadline(pid_t pid, int *status, int ms) {
    int waited = 0;
    while (waited < ms) {
        pid_t got = waitpid(pid, status, WNOHANG);
        if (got == pid) {
            return 0;
        }
        if (got < 0) {
            return -1;
        }
        usleep(20000);
        waited += 20;
    }
    kill(pid, SIGKILL);
    waitpid(pid, status, 0);
    return 1;
}

static int cmd_probe(int argc, char **argv) {
    const char *result = NULL;
    const char *attempt = NULL;
    const char *sibling = NULL;
    const char *published = NULL;
    const char *symlink_path = NULL;
    const char *socket_path = NULL;
    const char *abstract_name = NULL;
    const char *parent_procs = NULL;
    const char *cgroup_kill = NULL;
    const char *rename_dst = NULL;
    const char *link_dst = NULL;
    const char *regain_bin = NULL;
    const char *mode = "full";
    int executor_pid = 0;
    int sibling_pid = 0;
    const char *connects[16];
    int nconnect = 0;
    for (int i = 2; i < argc; i++) {
        if (!strcmp(argv[i], "--result") && i + 1 < argc) {
            result = argv[++i];
        } else if (!strcmp(argv[i], "--attempt") && i + 1 < argc) {
            attempt = argv[++i];
        } else if (!strcmp(argv[i], "--sibling") && i + 1 < argc) {
            sibling = argv[++i];
        } else if (!strcmp(argv[i], "--published") && i + 1 < argc) {
            published = argv[++i];
        } else if (!strcmp(argv[i], "--symlink") && i + 1 < argc) {
            symlink_path = argv[++i];
        } else if (!strcmp(argv[i], "--socket") && i + 1 < argc) {
            socket_path = argv[++i];
        } else if (!strcmp(argv[i], "--abstract") && i + 1 < argc) {
            abstract_name = argv[++i];
        } else if (!strcmp(argv[i], "--parent-procs") && i + 1 < argc) {
            parent_procs = argv[++i];
        } else if (!strcmp(argv[i], "--cgroup-kill") && i + 1 < argc) {
            cgroup_kill = argv[++i];
        } else if (!strcmp(argv[i], "--rename-dst") && i + 1 < argc) {
            rename_dst = argv[++i];
        } else if (!strcmp(argv[i], "--link-dst") && i + 1 < argc) {
            link_dst = argv[++i];
        } else if (!strcmp(argv[i], "--regain-bin") && i + 1 < argc) {
            regain_bin = argv[++i];
        } else if (!strcmp(argv[i], "--mode") && i + 1 < argc) {
            mode = argv[++i];
        } else if (!strcmp(argv[i], "--executor-pid") && i + 1 < argc) {
            executor_pid = atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--sibling-pid") && i + 1 < argc) {
            sibling_pid = atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--connect") && i + 1 < argc && nconnect < 16) {
            connects[nconnect++] = argv[++i];
        }
    }
    if (!result || !attempt) {
        fprintf(stderr, "probe missing result/attempt\n");
        return 2;
    }
    char started[512];
    snprintf(started, sizeof(started), "%s/tool_started", attempt);
    int sfd = open(started, O_CREAT | O_WRONLY | O_CLOEXEC, 0644);
    if (sfd >= 0) {
        if (dprintf(sfd, "1\n") < 0) {
            /* marker is best-effort */
        }
        close(sfd);
    }

    if (!strcmp(mode, "sleep")) {
        char parent_hb[512], child_hb[512], parent_pid[512], child_pid[512];
        snprintf(parent_hb, sizeof(parent_hb), "%s/parent.hb", attempt);
        snprintf(child_hb, sizeof(child_hb), "%s/child.hb", attempt);
        snprintf(parent_pid, sizeof(parent_pid), "%s/parent.pid", attempt);
        snprintf(child_pid, sizeof(child_pid), "%s/child.pid", attempt);
        pid_t mid = fork();
        if (mid == 0) {
            if (setsid() < 0) {
                _exit(93);
            }
            pid_t grand = fork();
            if (grand == 0) {
                signal(SIGHUP, SIG_IGN);
                signal(SIGTERM, SIG_IGN);
                FILE *f = fopen(child_pid, "w");
                if (f) {
                    fprintf(f, "%d\n", (int)getpid());
                    fclose(f);
                }
                /* 150 * 200ms: long enough for the container proof to cancel
                   and still bounded. The host harness cancels before this ends. */
                for (int i = 0; i < 150; i++) {
                    FILE *h = fopen(child_hb, "a");
                    if (h) {
                        fprintf(h, "%d\n", i);
                        fclose(h);
                    }
                    usleep(200000);
                }
                _exit(0);
            }
            _exit(0);
        }
        if (mid > 0) {
            int st = 0;
            wait_deadline(mid, &st, 2000);
        }
        FILE *f = fopen(parent_pid, "w");
        if (f) {
            fprintf(f, "%d\n", (int)getpid());
            fclose(f);
        }
        for (int i = 0; i < 150; i++) {
            FILE *h = fopen(parent_hb, "a");
            if (h) {
                fprintf(h, "%d\n", i);
                fclose(h);
            }
            usleep(200000);
        }
        return 0;
    }

    if (!strcmp(mode, "marker")) {
        char cred[256];
        char path[512];
        cred_detail(cred, sizeof(cred));
        if (snprintf(path, sizeof(path), "%s/parent.pid", attempt) >= (int)sizeof(path)) {
            return 96;
        }
        FILE *pidf = fopen(path, "w");
        if (!pidf) {
            perror(path);
            return 96;
        }
        fprintf(pidf, "%d\n", (int)getpid());
        fclose(pidf);
        if (snprintf(path, sizeof(path), "%s/parent.hb", attempt) >= (int)sizeof(path)) {
            return 96;
        }
        FILE *hb = fopen(path, "a");
        if (!hb) {
            perror(path);
            return 96;
        }
        fprintf(hb, "1\n");
        fclose(hb);
        FILE *out = fopen(result, "w");
        if (!out) {
            perror(result);
            return 96;
        }
        fprintf(out, "{\"cred\":\"");
        json_escape(out, cred);
        fprintf(out, "\"}\n");
        fclose(out);
        return 0;
    }

    if (!strcmp(mode, "child")) {
        FILE *out = fopen(result, "w");
        if (!out) {
            return 96;
        }
        int err = 0;
        int denied = sibling && try_open_read(sibling, &err) != 0 && is_perm(err);
        fprintf(out, "{\"sibling_denied\":%s,\"errno\":%d,\"class\":\"%s\"}\n", denied ? "true" : "false",
                err, err_class(denied ? 0 : (err == 0), err));
        fclose(out);
        return denied ? 0 : 97;
    }

    if (!strcmp(mode, "connect")) {
        FILE *out = fopen(result, "w");
        if (!out) {
            return 96;
        }
        fprintf(out, "{\"connects\":[");
        for (int i = 0; i < nconnect; i++) {
            char spec[128];
            snprintf(spec, sizeof(spec), "%s", connects[i]);
            char *colon = strrchr(spec, ':');
            int ok = 0;
            int err = 0;
            if (!colon) {
                err = EINVAL;
            } else {
                *colon = '\0';
                int port = atoi(colon + 1);
                int fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
                if (fd < 0) {
                    err = errno;
                } else {
                    struct sockaddr_in addr;
                    memset(&addr, 0, sizeof(addr));
                    addr.sin_family = AF_INET;
                    addr.sin_port = htons((uint16_t)port);
                    if (inet_pton(AF_INET, spec, &addr.sin_addr) != 1) {
                        err = EINVAL;
                    } else {
                        int rc = connect_timeout(fd, (struct sockaddr *)&addr, sizeof(addr), CONNECT_MS, &err);
                        ok = rc == 0;
                    }
                    close(fd);
                }
            }
            if (i) {
                fputc(',', out);
            }
            fprintf(out, "{\"target\":\"");
            json_escape(out, connects[i]);
            fprintf(out, "\",\"ok\":%s,\"errno\":%d,\"class\":\"%s\"}", ok ? "true" : "false", err,
                    err_class(ok, err));
        }
        fprintf(out, "]}\n");
        fclose(out);
        return 0;
    }

    FILE *out = fopen(result, "w");
    if (!out) {
        perror(result);
        return 96;
    }
    fprintf(out, "{\"checks\":[");
    int first = 1;
#define CHECK(name, pass, detail) check(out, &first, (name), (pass), (detail))
    char detail[320];
    int signal_only = !strcmp(mode, "signal");
    int cgroup_only = !strcmp(mode, "cgroup");
    if (!signal_only && !cgroup_only) {
        cred_detail(detail, sizeof(detail));
        uid_t r, e, s;
        gid_t gr, ge, gs;
        getresuid(&r, &e, &s);
        getresgid(&gr, &ge, &gs);
        int groups = getgroups(0, NULL);
        struct cap_header hdr = {.version = CAP_VERSION_3, .pid = 0};
        struct cap_data data[2];
        memset(data, 0, sizeof(data));
        syscall(__NR_capget, &hdr, data);
        int bound = 0;
        for (int cap = 0; cap < 64; cap++) {
            if (prctl(PR_CAPBSET_READ, cap, 0, 0, 0) == 1) {
                bound++;
            }
        }
        CHECK("tool_resuid_10003", r == 10003 && e == 10003 && s == 10003, detail);
        CHECK("tool_resgid_10003", gr == 10003 && ge == 10003 && gs == 10003, detail);
        CHECK("tool_no_supplementary_groups", groups == 0, detail);
        CHECK("tool_caps_clear",
              data[0].effective == 0 && data[0].permitted == 0 && data[0].inheritable == 0 &&
                  data[1].effective == 0 && data[1].permitted == 0 && data[1].inheritable == 0,
              detail);
        CHECK("tool_bounding_clear", bound == 0, detail);
        CHECK("tool_nnp", prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) == 1, detail);

        errno = 0;
        int regain_uid = setresuid(0, 0, 0);
        int regain_uid_err = errno;
        snprintf(detail, sizeof(detail), "class=%s errno=%d", err_class(regain_uid == 0, regain_uid_err),
                 regain_uid_err);
        CHECK("cannot_setuid_0", regain_uid != 0 && regain_uid_err == EPERM, detail);
        errno = 0;
        int regain_exec = setresuid(10002, 10002, 10002);
        int regain_exec_err = errno;
        snprintf(detail, sizeof(detail), "class=%s errno=%d", err_class(regain_exec == 0, regain_exec_err),
                 regain_exec_err);
        CHECK("cannot_setuid_10002", regain_exec != 0 && regain_exec_err == EPERM, detail);
        struct cap_data gain[2];
        memset(gain, 0, sizeof(gain));
        gain[0].effective = (1U << CAP_SETUID);
        gain[0].permitted = (1U << CAP_SETUID);
        errno = 0;
        int regain_cap = (int)syscall(__NR_capset, &hdr, gain);
        CHECK("cannot_capset", regain_cap != 0, "capset");

        char own[512];
        snprintf(own, sizeof(own), "%s/input.txt", attempt);
        int own_err = 0;
        int own_ok = try_open_read(own, &own_err) == 0;
        char outpath[512];
        snprintf(outpath, sizeof(outpath), "%s/output.txt", attempt);
        int wfd = open(outpath, O_CREAT | O_WRONLY | O_TRUNC | O_CLOEXEC, 0644);
        int write_ok = 0;
        if (wfd >= 0) {
            write_ok = write(wfd, "tool-write\n", 11) == 11;
            close(wfd);
        }
        CHECK("own_read", own_ok, own);
        CHECK("own_write", write_ok, outpath);

        int sib_err = 0;
        int sib_read = sibling ? try_open_read(sibling, &sib_err) : -1;
        snprintf(detail, sizeof(detail), "class=%s errno=%d path=%s", err_class(sib_read == 0, sib_err),
                 sib_err, sibling ? sibling : "");
        CHECK("sibling_read_denied", sibling && sib_read != 0 && is_perm(sib_err), detail);
        errno = 0;
        int sw = open(sibling ? sibling : "", O_WRONLY | O_CLOEXEC);
        int sw_err = errno;
        if (sw >= 0) {
            close(sw);
        }
        snprintf(detail, sizeof(detail), "class=%s errno=%d", err_class(sw >= 0, sw_err), sw_err);
        CHECK("sibling_write_denied", sibling && sw < 0 && is_perm(sw_err), detail);
        errno = 0;
        int tr = sibling ? truncate(sibling, 0) : -1;
        int tr_err = errno;
        snprintf(detail, sizeof(detail), "class=%s errno=%d", err_class(tr == 0, tr_err), tr_err);
        CHECK("sibling_truncate_denied", sibling && tr != 0 && is_perm(tr_err), detail);
        if (!rename_dst) {
            rename_dst = "/tmp/sec08-missing-rename-dst";
        }
        errno = 0;
        int rn = rename(outpath, rename_dst);
        int rn_err = errno;
        int rename_denied = rn != 0 && (is_perm(rn_err) || rn_err == EXDEV);
        snprintf(detail, sizeof(detail), "class=%s errno=%d dst=%s",
                 rename_denied ? "permission" : err_class(rn == 0, rn_err), rn_err, rename_dst);
        CHECK("rename_escape_denied", rename_denied, detail);
        if (!link_dst) {
            link_dst = "/tmp/sec08-missing-link-dst";
        }
        errno = 0;
        int lk = link(outpath, link_dst);
        int lk_err = errno;
        int link_denied = lk != 0 && (is_perm(lk_err) || lk_err == EXDEV);
        snprintf(detail, sizeof(detail), "class=%s errno=%d dst=%s",
                 link_denied ? "permission" : err_class(lk == 0, lk_err), lk_err, link_dst);
        CHECK("link_escape_denied", link_denied, detail);
        int sym_err = 0;
        int sym_read = symlink_path ? try_open_read(symlink_path, &sym_err) : -1;
        snprintf(detail, sizeof(detail), "class=%s errno=%d", err_class(sym_read == 0, sym_err), sym_err);
        CHECK("symlink_escape_denied", symlink_path && sym_read != 0 && is_perm(sym_err), detail);
        int pub_err = 0;
        int pub_read = published ? try_open_read(published, &pub_err) : -1;
        snprintf(detail, sizeof(detail), "class=%s errno=%d", err_class(pub_read == 0, pub_err), pub_err);
        CHECK("published_read_denied", published && pub_read != 0 && is_perm(pub_err), detail);

        const char *canary = getenv("EXECUTOR_CANARY");
        CHECK("executor_env_absent", canary == NULL, canary ? "present" : "absent");
        char proc_self[64], proc_exec[64], proc_one[] = "/proc/1/environ";
        snprintf(proc_self, sizeof(proc_self), "/proc/self/environ");
        snprintf(proc_exec, sizeof(proc_exec), "/proc/%d/environ", executor_pid > 0 ? executor_pid : 1);
        int ps_err = 0, pe_err = 0, p1_err = 0;
        int ps = try_open_read(proc_self, &ps_err);
        int pe = try_open_read(proc_exec, &pe_err);
        int p1 = try_open_read(proc_one, &p1_err);
        snprintf(detail, sizeof(detail), "class=%s errno=%d", err_class(ps == 0, ps_err), ps_err);
        CHECK("proc_self_denied", ps != 0 && is_perm(ps_err), detail);
        snprintf(detail, sizeof(detail), "class=%s errno=%d", err_class(pe == 0, pe_err), pe_err);
        CHECK("proc_executor_denied", pe != 0 && is_perm(pe_err), detail);
        snprintf(detail, sizeof(detail), "class=%s errno=%d", err_class(p1 == 0, p1_err), p1_err);
        CHECK("proc_one_denied", p1 != 0 && is_perm(p1_err), detail);

        int sock_fd_found = 0;
        for (int fd = 0; fd < 64; fd++) {
            struct stat st;
            if (fstat(fd, &st) == 0 && S_ISSOCK(st.st_mode)) {
                sock_fd_found = 1;
            }
        }
        CHECK("no_inherited_socket_fd", !sock_fd_found, sock_fd_found ? "socket fd" : "none");
        int connected = 0;
        int sock_err = ENOENT;
        if (socket_path) {
            connected = connect_unix_path(socket_path, &sock_err) == 0;
        }
        snprintf(detail, sizeof(detail), "class=%s errno=%d layer=dac path=%s",
                 err_class(connected, sock_err), sock_err, socket_path ? socket_path : "");
        CHECK("control_socket_denied",
              socket_path && !connected && sock_err == EACCES && strcmp(err_class(connected, sock_err), "timeout") != 0,
              detail);
        int abs_ok = 0;
        int abs_err = ENOENT;
        if (abstract_name) {
            abs_ok = connect_abstract(abstract_name, &abs_err) == 0;
        }
        snprintf(detail, sizeof(detail), "class=%s errno=%d layer=landlock_scope_abstract_unix name=%s",
                 err_class(abs_ok, abs_err), abs_err, abstract_name ? abstract_name : "");
        CHECK("abstract_uds_outside_denied", abstract_name && !abs_ok && abs_err == EPERM, detail);

        char child_result[512];
        snprintf(child_result, sizeof(child_result), "%s/child_result.json", attempt);
        pid_t child = fork();
        if (child == 0) {
            if (setsid() < 0) {
                _exit(93);
            }
            char *argv_child[] = {(char *)argv[0], "probe", "--mode", "child", "--result", child_result,
                                  "--attempt", (char *)attempt, "--sibling",
                                  (char *)(sibling ? sibling : ""), NULL};
            execv(argv[0], argv_child);
            _exit(94);
        }
        int st = 0;
        int child_wait = 1;
        if (child > 0) {
            child_wait = wait_deadline(child, &st, 3000);
        }
        int child_denied = 0;
        FILE *cf = fopen(child_result, "r");
        if (cf) {
            char buf[160];
            size_t n = fread(buf, 1, sizeof(buf) - 1, cf);
            buf[n] = '\0';
            child_denied = strstr(buf, "\"sibling_denied\":true") != NULL;
            fclose(cf);
        }
        snprintf(detail, sizeof(detail), "child_denied=%d wait=%d", child_denied, child_wait);
        CHECK("fork_exec_setsid_still_denied", child_denied && child_wait == 0, detail);
        if (regain_bin) {
            char regain_out[512];
            snprintf(regain_out, sizeof(regain_out), "%s/regain.txt", attempt);
            pid_t helper = fork();
            if (helper == 0) {
                char *hv[] = {(char *)regain_bin, "regain-helper", regain_out, NULL};
                execv(regain_bin, hv);
                _exit(98);
            }
            if (helper > 0) {
                wait_deadline(helper, NULL, 2000);
            }
            FILE *rf = fopen(regain_out, "r");
            int stayed = 0;
            if (rf) {
                unsigned rr = 1, ee = 1, ss = 1;
                if (fscanf(rf, "%u %u %u", &rr, &ee, &ss) == 3) {
                    stayed = rr == 10003 && ee == 10003 && ss == 10003;
                }
                fclose(rf);
            }
            CHECK("setuid_binary_does_not_regain", stayed, regain_bin);
        }
    }

    if (!cgroup_only && (executor_pid > 0 || sibling_pid > 0 || signal_only)) {
        if (executor_pid > 0) {
            errno = 0;
            int kr = kill(executor_pid, 0);
            int ke = errno;
            snprintf(detail, sizeof(detail), "class=%s errno=%d sig=0 different_uid", err_class(kr == 0, ke), ke);
            CHECK("cannot_signal_executor", kr != 0 && ke == EPERM, detail);
        }
        if (sibling_pid > 0) {
            errno = 0;
            int kr = kill(sibling_pid, 0);
            int ke = errno;
            snprintf(detail, sizeof(detail),
                     "class=%s errno=%d sig=0 same_uid_cross_domain scope=LANDLOCK_SCOPE_SIGNAL",
                     err_class(kr == 0, ke), ke);
            CHECK("cannot_signal_sibling_same_uid", kr != 0 && ke == EPERM, detail);
        }
    }
    if (!signal_only && (parent_procs || cgroup_kill || cgroup_only)) {
        if (parent_procs) {
            errno = 0;
            int fd = open(parent_procs, O_WRONLY | O_CLOEXEC);
            int oe = errno;
            if (fd >= 0) {
                char pidbuf[32];
                int n = snprintf(pidbuf, sizeof(pidbuf), "%d\n", (int)getpid());
                errno = 0;
                ssize_t wr = write(fd, pidbuf, (size_t)n);
                if (wr < 0) {
                    oe = errno;
                }
                close(fd);
                snprintf(detail, sizeof(detail), "class=%s errno=%d write_rc=%zd", err_class(wr >= 0, oe), oe, wr);
                CHECK("cannot_open_parent_cgroup_procs", wr < 0 && is_perm(oe), detail);
            } else {
                snprintf(detail, sizeof(detail), "class=%s errno=%d", err_class(0, oe), oe);
                CHECK("cannot_open_parent_cgroup_procs", is_perm(oe), detail);
            }
        }
        if (cgroup_kill) {
            errno = 0;
            int fd = open(cgroup_kill, O_WRONLY | O_CLOEXEC);
            int oe = errno;
            snprintf(detail, sizeof(detail), "class=%s errno=%d path=%s", err_class(fd >= 0, oe), oe, cgroup_kill);
            CHECK("cannot_write_delegated_cgroup_kill", fd < 0 && is_perm(oe), detail);
            if (fd >= 0) {
                close(fd);
            }
        }
    }
    fprintf(out, "]}\n");
    fclose(out);
    return 0;
}

static int cmd_regain_helper(int argc, char **argv) {
    uid_t r, e, s;
    getresuid(&r, &e, &s);
    const char *path = argc >= 3 ? argv[2] : NULL;
    if (!path) {
        return 2;
    }
    FILE *f = fopen(path, "w");
    if (!f) {
        return 3;
    }
    fprintf(f, "%u %u %u\n", (unsigned)r, (unsigned)e, (unsigned)s);
    fclose(f);
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: sec08tool launch|probe|cancel|identity|regain-helper\n");
        return 2;
    }
    if (!strcmp(argv[1], "identity")) {
        return cmd_identity();
    }
    if (!strcmp(argv[1], "cancel")) {
        return cmd_cancel(argc, argv);
    }
    if (!strcmp(argv[1], "regain-helper")) {
        return cmd_regain_helper(argc, argv);
    }
    if (!strcmp(argv[1], "probe")) {
        return cmd_probe(argc, argv);
    }
    if (strcmp(argv[1], "launch") != 0) {
        fprintf(stderr, "unknown command\n");
        return 2;
    }
    struct launch_opts opt = {
        .tool_uid = 10003,
        .tool_gid = 10003,
    };
    int idx = 2;
    for (; idx < argc; idx++) {
        if (!strcmp(argv[idx], "--")) {
            idx++;
            break;
        } else if (!strcmp(argv[idx], "--attempt") && idx + 1 < argc) {
            opt.attempt = argv[++idx];
        } else if (!strcmp(argv[idx], "--binary") && idx + 1 < argc) {
            opt.binary = argv[++idx];
        } else if (!strcmp(argv[idx], "--status") && idx + 1 < argc) {
            opt.status_path = argv[++idx];
        } else if (!strcmp(argv[idx], "--cgroup-procs") && idx + 1 < argc) {
            opt.cgroup_procs = argv[++idx];
        } else if (!strcmp(argv[idx], "--control-dir") && idx + 1 < argc) {
            opt.control_dir = argv[++idx];
        } else if (!strcmp(argv[idx], "--ro") && idx + 1 < argc && opt.nro < 8) {
            opt.ro[opt.nro++] = argv[++idx];
        } else if (!strcmp(argv[idx], "--protected") && idx + 1 < argc && opt.nprot < 12) {
            opt.prot[opt.nprot++] = argv[++idx];
        } else if (!strcmp(argv[idx], "--uid") && idx + 1 < argc) {
            opt.tool_uid = (uid_t)atoi(argv[++idx]);
        } else if (!strcmp(argv[idx], "--gid") && idx + 1 < argc) {
            opt.tool_gid = (gid_t)atoi(argv[++idx]);
        } else if (!strcmp(argv[idx], "--force-abi")) {
            opt.force_abi = 1;
        } else if (!strcmp(argv[idx], "--force-ruleset")) {
            opt.force_ruleset = 1;
        } else {
            fprintf(stderr, "bad launch arg %s\n", argv[idx]);
            return 2;
        }
    }
    if (!opt.attempt || !opt.binary || idx >= argc) {
        fprintf(stderr, "launch requires --attempt --binary -- argv\n");
        return 2;
    }
    opt.exec_argv = &argv[idx];
    return cmd_launch(&opt);
}
