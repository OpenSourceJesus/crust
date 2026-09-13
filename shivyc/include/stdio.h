#ifdef _WIN64
typedef unsigned long long size_t;   /* LLP64: `long` is 4 bytes */
#else
typedef unsigned long size_t;
#endif
typedef struct __FILE_STRUCT FILE;

void     clearerr(FILE *);
char    *ctermid(char *);
int      fclose(FILE *);
FILE    *fdopen(int, const char *);
int      feof(FILE *);
int      ferror(FILE *);
int      fflush(FILE *);
int      fgetc(FILE*);
char    *fgets(char *, int, FILE*);
int      fileno(FILE*);
void     flockfile(FILE*);
FILE    *fopen(const char *, const char *);
int      fprintf(FILE *, const char *, ...);
int      fputc(int, FILE*);
int      fputs(const char *, FILE*);
size_t   fread(void *, size_t, size_t, FILE *);
FILE    *freopen(const char *, const char *, FILE *);
int      fscanf(); // vargargs not yet implemented
int      fseek(FILE *, long, int);
long     ftell(FILE *);
int      ftrylockfile(FILE *);
void     funlockfile(FILE *);
size_t fwrite(const void *, size_t, size_t, FILE*);
int      getc(FILE*);
int      getchar(void);
int      getc_unlocked(FILE *);
int      getchar_unlocked(void);
// removed in C11
// char    *gets(char *);
int      getw(void *);
int      pclose(void *);
void     perror(const char *);
void    *popen(const char *, const char *);
int      printf(const char *, ...);
int      putc(int, FILE*);
int      putchar(int);
int      putc_unlocked(int, FILE*);
int      putchar_unlocked(int);
int      puts(const char *);
int      putw(int, FILE*);
int      remove(const char *);
int      rename(const char *, const char *);
void     rewind(FILE *);
int      scanf(); // vargargs not yet implemented
void     setbuf(FILE*, char *);
int      setvbuf(FILE*, char *, int, size_t);
int      snprintf(char *, size_t, const char *, ...);
int      sprintf(char *, const char *, ...);
int      sscanf(); // vargargs not yet implemented
char    *tempnam(const char *, const char *);
FILE    *tmpfile(void);
char    *tmpnam(char *);
int      ungetc(int, FILE*);

#if defined(__APPLE__) || defined(__FreeBSD__)
/* The BSD libcs (Apple's libSystem, FreeBSD's libc.so.7) export the standard
 * streams as __stdinp / __stdoutp / __stderrp; their <stdio.h> maps the
 * names with these same macros. A bare `extern stdout` would be an undefined
 * symbol at link time. */
extern void* __stdinp;
extern void* __stdoutp;
extern void* __stderrp;
#define stdin  __stdinp
#define stdout __stdoutp
#define stderr __stderrp
#elif defined(_WIN64)
/* msvcrt.dll keeps the standard streams as the first three entries of its
 * `_iob` array, and hands out the array's address from __iob_func(). A
 * function is used rather than the `_iob` variable itself because data
 * exported from a DLL can only be reached through the import table, and a
 * function needs no such care. Indexing needs the real element size, so
 * FILE is msvcrt's 48-byte `struct _iobuf` here rather than opaque. */
struct __FILE_STRUCT {
    char *_ptr;
    int   _cnt;
    char *_base;
    int   _flag;
    int   _file;
    int   _charbuf;
    int   _bufsiz;
    char *_tmpfname;
};
FILE *__iob_func(void);
#define stdin  (&__iob_func()[0])
#define stdout (&__iob_func()[1])
#define stderr (&__iob_func()[2])
int vprintf(const char *, char *);
int vfprintf(FILE *, const char *, char *);
int vsprintf(char *, const char *, char *);
int vsnprintf(char *, size_t, const char *, char *);
#define EOF (-1)
#define SEEK_SET 0
#define SEEK_CUR 1
#define SEEK_END 2
#define BUFSIZ 512
#else
extern void* stdin;
extern void* stdout;
extern void* stderr;
#endif
