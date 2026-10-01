#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <linux/landlock.h>
#include <netinet/in.h>
#include <signal.h>
#include <sys/un.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
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

#define MIN_ABI 3
#define CAP_SETGID 6
#define CAP_SETUID 7
#define CAP_SETPCAP 8
#define CAP_VERSION_3 0x20080522

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
    int rc = (int)syscall(__NR_landlock_create_ruleset, NULL, 0,
                           LANDLOCK_CREATE_RULESET_VERSION);
    return rc;
}

static uint64_t handled_fs(int abi) {
    uint64_t mask =
        LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_WRITE_FILE |
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
    int rc = (int)syscall(__NR_landlock_add_rule, ruleset, LANDLOCK_RULE_PATH_BENEATH,
                           &attr, 0);
    int saved = errno;
    close(fd);
    errno = saved;
    return rc;
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
        if (fd == dirfd || fd == STDIN_FILENO || fd == STDOUT_FILENO ||
            fd == STDERR_FILENO) {
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
    fprintf(out, "resuid %u %u %u\nresgid %u %u %u\nnnp %d\nseccomp %d\n",
            (unsigned)r, (unsigned)e, (unsigned)s, (unsigned)gr, (unsigned)ge,
            (unsigned)gs, prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0),
            prctl(PR_GET_SECCOMP, 0, 0, 0, 0));
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

static int apply_domain(const char *attempt, const char *binary) {
    if (g_abi < MIN_ABI) {
        fprintf(stderr, "landlock abi %d below required %d\n", g_abi, MIN_ABI);
        return -1;
    }
    struct landlock_ruleset_attr attr;
    memset(&attr, 0, sizeof(attr));
    attr.handled_access_fs = handled_fs(g_abi);
    /* Pass only the filesystem word so network access stays outside Landlock. */
    int ruleset = (int)syscall(__NR_landlock_create_ruleset, &attr,
                                sizeof(attr.handled_access_fs), 0);
    if (ruleset < 0) {
        perror("landlock_create_ruleset");
        return -1;
    }
    uint64_t work = LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_WRITE_FILE |
                    LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR |
                    LANDLOCK_ACCESS_FS_REMOVE_DIR | LANDLOCK_ACCESS_FS_REMOVE_FILE |
                    LANDLOCK_ACCESS_FS_MAKE_REG | LANDLOCK_ACCESS_FS_MAKE_DIR |
                    LANDLOCK_ACCESS_FS_TRUNCATE | LANDLOCK_ACCESS_FS_REFER;
    uint64_t ro = LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE |
                  LANDLOCK_ACCESS_FS_READ_DIR;
    if (add_path(ruleset, attempt, work) != 0) {
        perror(attempt);
        close(ruleset);
        return -1;
    }
    const char *dirs[] = {"/usr", "/lib", "/lib64", "/bin", "/sbin", NULL};
    for (int i = 0; dirs[i]; i++) {
        if (access(dirs[i], F_OK) != 0) {
            continue;
        }
        if (add_path(ruleset, dirs[i], ro) != 0) {
            perror(dirs[i]);
            close(ruleset);
            return -1;
        }
    }
    char bin_dir[512];
    snprintf(bin_dir, sizeof(bin_dir), "%s", binary);
    char *slash = strrchr(bin_dir, '/');
    if (slash && slash != bin_dir) {
        *slash = '\0';
        if (add_path(ruleset, bin_dir, ro) != 0) {
            perror(bin_dir);
            close(ruleset);
            return -1;
        }
    }
    if (access("/dev/null", F_OK) == 0) {
        add_path(ruleset, "/dev/null",
                 LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_WRITE_FILE);
    }
    if (access("/dev/urandom", F_OK) == 0) {
        add_path(ruleset, "/dev/urandom", LANDLOCK_ACCESS_FS_READ_FILE);
    }
    if (access("/etc/ld.so.cache", F_OK) == 0) {
        add_path(ruleset, "/etc/ld.so.cache", LANDLOCK_ACCESS_FS_READ_FILE);
    }
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) {
        perror("PR_SET_NO_NEW_PRIVS");
        close(ruleset);
        return -1;
    }
    if (syscall(__NR_landlock_restrict_self, ruleset, 0) != 0) {
        perror("landlock_restrict_self");
        close(ruleset);
        return -1;
    }
    close(ruleset);
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
    printf("{\"abi\":%d,\"min_abi\":%d,\"euid\":%u}\n", abi, MIN_ABI, (unsigned)geteuid());
    return abi >= MIN_ABI ? 0 : 90;
}

struct launch_opts {
    const char *attempt;
    const char *binary;
    const char *status_path;
    const char *cgroup_procs;
    uid_t tool_uid;
    gid_t tool_gid;
    int force_abi;
    int force_ruleset;
    char **exec_argv;
};

static int cmd_launch(struct launch_opts *opt) {
    g_abi = landlock_abi();
    fprintf(stderr, "entrypoint euid=%u abi=%d\n", (unsigned)geteuid(), g_abi);
    if (opt->force_abi || g_abi < MIN_ABI) {
        fprintf(stderr, "refuse exec: abi %d required %d force=%d\n", g_abi, MIN_ABI,
                opt->force_abi);
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
    }
    if (opt->force_ruleset) {
        if (syscall(__NR_landlock_restrict_self, -1, 0) == 0) {
            fprintf(stderr, "unexpected restrict success\n");
        }
        fprintf(stderr, "refuse exec: forced ruleset failure\n");
        return 91;
    }
    if (apply_domain(opt->attempt, opt->binary) != 0) {
        fprintf(stderr, "refuse exec: domain failed\n");
        return 92;
    }
    drop_bounding_set();
    become_tool(opt->tool_uid, opt->tool_gid);
    exec_clean(opt->binary, opt->exec_argv);
    return 84;
}

static void json_escape(FILE *out, const char *s) {
    for (; *s; s++) {
        if (*s == '"' || *s == '\\') {
            fputc('\\', out);
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

static int try_open_read(const char *path) {
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        return -1;
    }
    char buf[8];
    ssize_t n = read(fd, buf, sizeof(buf));
    close(fd);
    return n >= 0 ? 0 : -1;
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
    snprintf(buf, n,
             "uid %u %u %u gid %u %u %u groups %d cap %08x/%08x/%08x bound %d nnp %d",
             (unsigned)r, (unsigned)e, (unsigned)s, (unsigned)gr, (unsigned)ge,
             (unsigned)gs, groups, data[0].effective, data[0].permitted,
             data[0].inheritable, bound, prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0));
}

static int cmd_probe(int argc, char **argv) {
    const char *result = NULL;
    const char *attempt = NULL;
    const char *sibling = NULL;
    const char *published = NULL;
    const char *symlink_path = NULL;
    const char *socket_path = NULL;
        const char *parent_procs = NULL;
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
        } else if (!strcmp(argv[i], "--parent-procs") && i + 1 < argc) {
            parent_procs = argv[++i];
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
        dprintf(sfd, "1\n");
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
            waitpid(mid, NULL, 0);
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

    if (!strcmp(mode, "child")) {
        FILE *out = fopen(result, "w");
        if (!out) {
            return 96;
        }
        int denied = sibling && try_open_read(sibling) != 0;
        fprintf(out, "{\"sibling_denied\":%s}\n", denied ? "true" : "false");
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
                int fd = socket(AF_INET, SOCK_STREAM, 0);
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
                        struct timeval tv = {.tv_sec = 1, .tv_usec = 0};
                        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
                        if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0) {
                            ok = 1;
                        } else {
                            err = errno;
                        }
                    }
                    close(fd);
                }
            }
            if (i) {
                fputc(',', out);
            }
            fprintf(out, "{\"target\":\"");
            json_escape(out, connects[i]);
            fprintf(out, "\",\"ok\":%s,\"errno\":%d}", ok ? "true" : "false", err);
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
    char detail[256];
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
    CHECK("cannot_setuid_0", regain_uid != 0 && regain_uid_err == EPERM, "setuid 0");
    errno = 0;
    int regain_exec = setresuid(10002, 10002, 10002);
    CHECK("cannot_setuid_10002", regain_exec != 0 && errno == EPERM, "setuid 10002");
    struct cap_data gain[2];
    memset(gain, 0, sizeof(gain));
    gain[0].effective = (1U << CAP_SETUID);
    gain[0].permitted = (1U << CAP_SETUID);
    errno = 0;
    int regain_cap = (int)syscall(__NR_capset, &hdr, gain);
    CHECK("cannot_capset", regain_cap != 0, "capset");

    char own[512];
    snprintf(own, sizeof(own), "%s/input.txt", attempt);
    int own_ok = try_open_read(own) == 0;
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

    CHECK("sibling_read_denied", sibling && try_open_read(sibling) != 0, sibling);
    errno = 0;
    int sw = open(sibling ? sibling : "", O_WRONLY | O_CLOEXEC);
    CHECK("sibling_write_denied", sibling && sw < 0, sibling);
    if (sw >= 0) {
        close(sw);
    }
    errno = 0;
    int tr = sibling ? truncate(sibling, 0) : -1;
    CHECK("sibling_truncate_denied", sibling && tr != 0, sibling);
    if (!rename_dst) {
        rename_dst = "/tmp/sec08-missing-rename-dst";
    }
    errno = 0;
    int rn = rename(outpath, rename_dst);
    CHECK("rename_escape_denied", rn != 0, rename_dst);
    if (!link_dst) {
        link_dst = "/tmp/sec08-missing-link-dst";
    }
    errno = 0;
    int lk = link(outpath, link_dst);
    CHECK("link_escape_denied", lk != 0, link_dst);
    CHECK("symlink_escape_denied", symlink_path && try_open_read(symlink_path) != 0,
          symlink_path ? symlink_path : "");
    CHECK("published_read_denied", published && try_open_read(published) != 0,
          published ? published : "");

    const char *canary = getenv("EXECUTOR_CANARY");
    CHECK("executor_env_absent", canary == NULL, canary ? "present" : "absent");
    char proc_self[64], proc_exec[64], proc_one[] = "/proc/1/environ";
    snprintf(proc_self, sizeof(proc_self), "/proc/self/environ");
    snprintf(proc_exec, sizeof(proc_exec), "/proc/%d/environ", executor_pid > 0 ? executor_pid : 1);
    CHECK("proc_self_denied", try_open_read(proc_self) != 0, proc_self);
    CHECK("proc_executor_denied", try_open_read(proc_exec) != 0, proc_exec);
    CHECK("proc_one_denied", try_open_read(proc_one) != 0, proc_one);

    int sock_fd_found = 0;
    for (int fd = 0; fd < 64; fd++) {
        struct stat st;
        if (fstat(fd, &st) == 0 && S_ISSOCK(st.st_mode)) {
            sock_fd_found = 1;
        }
    }
    CHECK("no_inherited_socket_fd", !sock_fd_found, sock_fd_found ? "socket fd" : "none");
    errno = 0;
    int connected = 0;
    if (socket_path) {
        int fd = socket(AF_UNIX, SOCK_STREAM, 0);
        if (fd >= 0) {
            struct sockaddr_un addr;
            memset(&addr, 0, sizeof(addr));
            addr.sun_family = AF_UNIX;
            snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", socket_path);
            if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0) {
                connected = 1;
            }
            close(fd);
        }
    }
    CHECK("control_socket_denied", socket_path && !connected, socket_path ? socket_path : "");

    if (executor_pid > 0) {
        errno = 0;
        int kr = kill(executor_pid, SIGTERM);
        int ke = errno;
        snprintf(detail, sizeof(detail), "rc %d errno %d", kr, ke);
        CHECK("cannot_signal_executor", kr != 0 && ke == EPERM, detail);
    }
    if (sibling_pid > 0) {
        errno = 0;
        int kr = kill(sibling_pid, SIGTERM);
        int ke = errno;
        snprintf(detail, sizeof(detail), "rc %d errno %d", kr, ke);
        CHECK("cannot_signal_sibling_same_uid", kr != 0 && ke == EPERM, detail);
    }
    if (parent_procs) {
        errno = 0;
        int fd = open(parent_procs, O_WRONLY | O_CLOEXEC);
        int oe = errno;
        snprintf(detail, sizeof(detail), "open rc %d errno %d", fd, oe);
        CHECK("cannot_open_parent_cgroup_procs", fd < 0, detail);
        if (fd >= 0) {
            close(fd);
        }
    }

    char child_result[512];
    snprintf(child_result, sizeof(child_result), "%s/child_result.json", attempt);
    pid_t child = fork();
    if (child == 0) {
        if (setsid() < 0) {
            _exit(93);
        }
        char *argv_child[] = {(char *)argv[0], "probe", "--mode", "child", "--result",
                              child_result, "--attempt", (char *)attempt, "--sibling",
                              (char *)(sibling ? sibling : ""), NULL};
        execv(argv[0], argv_child);
        _exit(94);
    }
    int st = 0;
    if (child > 0) {
        waitpid(child, &st, 0);
    }
    int child_denied = 0;
    FILE *cf = fopen(child_result, "r");
    if (cf) {
        char buf[128];
        size_t n = fread(buf, 1, sizeof(buf) - 1, cf);
        buf[n] = '\0';
        child_denied = strstr(buf, "\"sibling_denied\":true") != NULL;
        fclose(cf);
    }
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
            waitpid(helper, NULL, 0);
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
    CHECK("fork_exec_setsid_still_denied", child_denied, "child result");
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
