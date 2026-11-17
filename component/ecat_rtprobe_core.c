//
//    ecat_rtprobe: the file (see ecat_rtprobe_core.h). No HAL here, so
//    that tests/test_rtprobe.c runs it without LinuxCNC.
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include "ecat_rtprobe_core.h"

#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

int ecat_rtprobe_open(ecat_rtprobe_t *p, const char *path, const char *const *names, unsigned n, int64_t now) {
  size_t size;
  void *m;
  int fd;
  unsigned i;

  p->head = NULL;
  p->size = 0;
  if (n == 0 || n > ECAT_RTPROBE_MAX) return -EINVAL;
  for (i = 0; i < n; i++) {
    if (names[i] == NULL || !names[i][0] || strlen(names[i]) >= ECAT_RTPROBE_NAME_LEN) return -EINVAL;
  }
  size = ecat_rtprobe_size(n);
  // a new file each time: a reader of an earlier one keeps its own mapping
  unlink(path);
  fd = open(path, O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0644);
  if (fd < 0) return -errno;
  if (ftruncate(fd, (off_t)size) < 0) {
    int e = -errno;
    close(fd);
    unlink(path);
    return e;
  }
  m = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  close(fd);
  if (m == MAP_FAILED) {
    int e = -errno;
    unlink(path);
    return e;
  }
  // the ring is touched once here, not in the first real-time runs
  memset(m, 0, size);
  p->head = m;
  p->size = size;
  for (i = 0; i < n; i++) {
    strcpy(ecat_rtprobe_inst(p, i)->name, names[i]);
  }
  p->head->n = n;
  p->head->slots = ECAT_RTPROBE_SLOTS;
  p->head->inst_size = sizeof(ecat_rtprobe_inst_t);
  p->head->created = now;
  p->head->version = ECAT_RTPROBE_VERSION;
  // the magic last: a reader that sees it sees a whole header
  __atomic_thread_fence(__ATOMIC_RELEASE);
  memcpy(p->head->magic, ECAT_RTPROBE_MAGIC, sizeof(ECAT_RTPROBE_MAGIC));
  return 0;
}

void ecat_rtprobe_close(ecat_rtprobe_t *p, const char *path) {
  if (p->head != NULL) {
    munmap(p->head, p->size);
    p->head = NULL;
  }
  if (path != NULL) unlink(path);
}
