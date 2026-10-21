/* Linux-only, non-setuid, statically linked protected signer bootstrap.
 *
 * The reviewed installed binary is the first executable to receive signer
 * values. Compile UID selection is a separate operator installation decision;
 * the unconfigured source build always refuses. Never replace -static with a
 * dynamic wrapper: loader environment injection precedes main().
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#ifndef ELSPETH_OPERATOR_UID
#define ELSPETH_OPERATOR_UID (-1)
#endif
#ifndef ELSPETH_CANDIDATE_UID
#define ELSPETH_CANDIDATE_UID (-1)
#endif

#define ARTIFACT_ROOT "/opt/elspeth/operator"
#define ENTRYPOINT "/usr/local/libexec/elspeth-operator"
#define INTERPRETER "/usr/bin/python3.12"
#define LOADER "/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
#define LIBRARY_ROOT ARTIFACT_ROOT "/platform/lib"
#define STDLIB "/usr/lib/python3.12"
#define BOOTSTRAP ARTIFACT_ROOT "/bootstrap.py"
#define MAX_PACKET 1048576U
#define MAX_SELECTED 64U

static size_t packet_bytes;

static void refuse(const char *reason) {
    fprintf(stderr, "elspeth-operator: %s; no signer action started.\n", reason);
    exit(78);
}

static void check_object(const char *path, int directory) {
    struct stat metadata;
    if (lstat(path, &metadata) != 0 || metadata.st_uid != 0 ||
        (metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0 ||
        (directory ? !S_ISDIR(metadata.st_mode) : !S_ISREG(metadata.st_mode)))
        refuse("protected startup path authentication failed");
}

static void check_path(const char *path, int directory) {
    char buffer[PATH_MAX];
    size_t length = strlen(path);
    if (path[0] != '/' || length >= sizeof(buffer))
        refuse("invalid protected startup path");
    memcpy(buffer, path, length + 1);
    check_object("/", 1);
    for (size_t i = 1; i < length; i++) {
        if (buffer[i] == '/') {
            buffer[i] = '\0';
            check_object(buffer, 1);
            buffer[i] = '/';
        }
    }
    check_object(buffer, directory);
}

static void check_tree(const char *path, unsigned int depth) {
    if (depth > 128)
        refuse("protected startup tree is too deep");
    check_path(path, 1);
    DIR *stream = opendir(path);
    if (stream == NULL)
        refuse("protected startup tree cannot be read");
    struct dirent *entry;
    errno = 0;
    while ((entry = readdir(stream)) != NULL) {
        if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0)
            continue;
        char child[PATH_MAX];
        if (snprintf(child, sizeof(child), "%s/%s", path, entry->d_name) >= (int)sizeof(child))
            refuse("protected startup path is too long");
        struct stat metadata;
        if (lstat(child, &metadata) != 0)
            refuse("protected startup tree changed");
        if (S_ISDIR(metadata.st_mode))
            check_tree(child, depth + 1);
        else
            check_path(child, 0);
        errno = 0;
    }
    if (errno != 0 || closedir(stream) != 0)
        refuse("protected startup tree read failed");
}

/* Ubuntu's OS library directories use symlinks. Authenticate both link custody
 * and every resolved target ancestor; candidate-owned targets never qualify. */
static void check_resolved_path(const char *path, int directory, unsigned int depth) {
    if (depth > 64 || path[0] != '/' || strlen(path) >= PATH_MAX)
        refuse("unsupported platform library path");
    char prefix[PATH_MAX];
    strcpy(prefix, path);
    size_t length = strlen(prefix);
    check_object("/", 1);
    for (size_t i = 1; i <= length; i++) {
        if (prefix[i] != '/' && prefix[i] != '\0')
            continue;
        char saved = prefix[i];
        prefix[i] = '\0';
        struct stat metadata;
        if (lstat(prefix, &metadata) != 0 || metadata.st_uid != 0)
            refuse("platform library ownership is unsafe");
        int is_directory = i < length || directory;
        if (S_ISLNK(metadata.st_mode)) {
            char resolved[PATH_MAX];
            if (realpath(prefix, resolved) == NULL)
                refuse("platform library target is unavailable");
            check_resolved_path(resolved, is_directory, depth + 1);
        } else {
            check_object(prefix, is_directory);
        }
        prefix[i] = saved;
    }
}

static void check_library_root(const char *path) {
    check_resolved_path(path, 1, 0);
    DIR *stream = opendir(path);
    if (stream == NULL)
        refuse("platform library root is unavailable");
    struct dirent *entry;
    errno = 0;
    while ((entry = readdir(stream)) != NULL) {
        if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0)
            continue;
        char child[PATH_MAX];
        if (snprintf(child, sizeof(child), "%s/%s", path, entry->d_name) >= (int)sizeof(child))
            refuse("platform library path is too long");
        struct stat metadata;
        if (stat(child, &metadata) != 0)
            refuse("platform library target is unavailable");
        /* glibc-hwcaps search is disabled below. The fixed Ubuntu profile must
         * independently qualify exact NEEDED/RPATH/dlopen dependency closure. */
        check_resolved_path(child, S_ISDIR(metadata.st_mode), 0);
        errno = 0;
    }
    if (errno != 0 || closedir(stream) != 0)
        refuse("platform library root read failed");
}

static void packet_write(int fd, const void *bytes, size_t count) {
    if (count > MAX_PACKET - packet_bytes)
        refuse("operator startup handoff is too large");
    packet_bytes += count;
    const unsigned char *position = bytes;
    while (count != 0) {
        ssize_t written = write(fd, position, count);
        if (written <= 0)
            refuse("operator startup handoff write failed");
        position += written;
        count -= (size_t)written;
    }
}

static void packet_integer(int fd, uint32_t value) {
    uint32_t encoded = htonl(value);
    packet_write(fd, &encoded, sizeof(encoded));
}

static void packet_string(int fd, const char *value) {
    size_t length = strlen(value);
    if (length > MAX_PACKET)
        refuse("operator startup string is too large");
    packet_integer(fd, (uint32_t)length);
    packet_write(fd, value, length);
}

static void select_name(const char **names, unsigned int *count, const char *name) {
    if (name[0] == '\0' || strchr(name, '=') != NULL)
        refuse("invalid selected signer variable name");
    for (unsigned int i = 0; i < *count; i++)
        if (strcmp(names[i], name) == 0)
            return;
    if (*count >= MAX_SELECTED)
        refuse("too many selected signer variables");
    names[(*count)++] = name;
}

int main(int argc, char **argv) {
    if (ELSPETH_OPERATOR_UID < 0 || ELSPETH_CANDIDATE_UID < 0 ||
        ELSPETH_OPERATOR_UID == ELSPETH_CANDIDATE_UID)
        refuse("unqualified operator runtime principal configuration");
    if (getuid() != (uid_t)ELSPETH_OPERATOR_UID || geteuid() != getuid() ||
        getgid() != getegid())
        refuse("operator principal is not authorized");
    if (argc < 2 || argc > 1025)
        refuse("operator invocation requires a bounded explicit operation");

    char executable[PATH_MAX];
    ssize_t length = readlink("/proc/self/exe", executable, sizeof(executable) - 1);
    if (length < 0)
        refuse("native executable identity is unavailable");
    executable[length] = '\0';
    if (strcmp(executable, ENTRYPOINT) != 0)
        refuse("native executable is not the installed entrypoint");
    check_path(ENTRYPOINT, 0);
    check_path(INTERPRETER, 0);
    check_path(LOADER, 0);
    check_tree(STDLIB, 0);
    check_tree(ARTIFACT_ROOT, 0);
    check_tree(LIBRARY_ROOT, 0);
    check_library_root("/lib");
    check_library_root("/usr/lib");
    check_library_root("/lib/x86_64-linux-gnu");
    check_library_root("/usr/lib/x86_64-linux-gnu");
    check_path(ARTIFACT_ROOT "/runtime-manifest.json", 0);
    check_path(BOOTSTRAP, 0);
    /* Cache use is disabled. A preload file can select code despite closedenv,
     * so this profile refuses its presence rather than trusting its contents. */
    struct stat preload;
    if (lstat("/etc/ld.so.preload", &preload) == 0)
        refuse("system loader preload is unsupported");
    else if (errno != ENOENT)
        refuse("system loader configuration is unavailable");

    const char *selected[MAX_SELECTED];
    unsigned int selected_count = 0;
    select_name(selected, &selected_count, "ELSPETH_JUDGE_METADATA_HMAC_KEY");
    select_name(selected, &selected_count, "ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE");
    select_name(selected, &selected_count, "ELSPETH_JUDGE_OVERRIDE_TOKEN");
    select_name(selected, &selected_count, "ELSPETH_JUDGE_OVERRIDE_TOKEN_SHA256");
    const char *options[] = {"--old-key-env", "--new-key-env"};
    for (int i = 1; i < argc; i++) {
        for (size_t j = 0; j < sizeof(options) / sizeof(options[0]); j++) {
            size_t option_length = strlen(options[j]);
            if (strcmp(argv[i], options[j]) == 0 && i + 1 < argc)
                select_name(selected, &selected_count, argv[i + 1]);
            else if (strncmp(argv[i], options[j], option_length) == 0 && argv[i][option_length] == '=')
                select_name(selected, &selected_count, argv[i] + option_length + 1);
        }
    }

    char cwd[PATH_MAX];
    if (getcwd(cwd, sizeof(cwd)) == NULL)
        refuse("operator invocation directory is unavailable");
    struct rlimit core_limit = {0, 0};
    if (setrlimit(RLIMIT_CORE, &core_limit) != 0 ||
        prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 ||
        prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0)
        refuse("operator process protection is unavailable");
    int fd = memfd_create("elspeth-operator-startup", MFD_ALLOW_SEALING);
    if (fd < 0)
        refuse("sealed operator startup channel is unavailable");
    packet_write(fd, "ELSPETHOP1\0", 11);
    packet_string(fd, ARTIFACT_ROOT);
    packet_string(fd, cwd);
    packet_integer(fd, (uint32_t)ELSPETH_OPERATOR_UID);
    packet_integer(fd, (uint32_t)ELSPETH_CANDIDATE_UID);
    packet_integer(fd, (uint32_t)(argc - 1));
    for (int i = 1; i < argc; i++)
        packet_string(fd, argv[i]);
    unsigned int present = 0;
    for (unsigned int i = 0; i < selected_count; i++)
        if (getenv(selected[i]) != NULL)
            present++;
    packet_integer(fd, present);
    for (unsigned int i = 0; i < selected_count; i++) {
        const char *value = getenv(selected[i]);
        if (value != NULL) {
            packet_string(fd, selected[i]);
            packet_string(fd, value);
        }
    }
    if (fcntl(fd, F_ADD_SEALS, F_SEAL_SEAL | F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE) != 0)
        refuse("operator startup channel sealing failed");
    if (fd != 3) {
        if (dup2(fd, 3) < 0)
            refuse("operator startup channel transfer failed");
        close(fd);
    }
    if (close_range(4, ~0U, 0) != 0)
        refuse("inherited descriptor closure failed");
    if (chdir(ARTIFACT_ROOT) != 0)
        refuse("protected runtime directory is unavailable");
    char **python_argv = calloc((size_t)argc + 12, sizeof(char *));
    if (python_argv == NULL)
        refuse("operator startup allocation failed");
    python_argv[0] = LOADER;
    python_argv[1] = "--inhibit-cache";
    python_argv[2] = "--library-path";
    python_argv[3] = LIBRARY_ROOT;
    python_argv[4] = "--glibc-hwcaps-mask";
    python_argv[5] = "";
    python_argv[6] = INTERPRETER;
    python_argv[7] = "-I";
    python_argv[8] = "-S";
    python_argv[9] = "-B";
    python_argv[10] = BOOTSTRAP;
    for (int i = 1; i < argc; i++)
        python_argv[i + 10] = argv[i];
    char *closed_environment[] = {
        "PATH=/usr/bin:/bin", "LANG=C.UTF-8", "LC_ALL=C.UTF-8", "PYTHON_DOTENV_DISABLED=1", NULL
    };
    execve(LOADER, python_argv, closed_environment);
    refuse("protected interpreter execution failed");
    return 78;
}
