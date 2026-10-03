#pragma once
#if !defined(_WIN32)
#include <cstring>
#include <cstddef>
inline int strcpy_s(char* dst, size_t dstsz, const char* src) {
    if (!dst || dstsz == 0) return 22;
    if (!src) { dst[0] = '\0'; return 22; }
    size_t n = std::strlen(src);
    if (n >= dstsz) n = dstsz - 1;
    std::memcpy(dst, src, n); dst[n] = '\0';
    return 0;
}
inline int strncpy_s(char* dst, size_t dstsz, const char* src, size_t count) {
    if (!dst || dstsz == 0) return 22;
    if (!src) { dst[0] = '\0'; return 22; }
    size_t n = std::strlen(src);
    if (n > count) n = count;
    if (n >= dstsz) n = dstsz - 1;
    std::memcpy(dst, src, n); dst[n] = '\0';
    return 0;
}
#endif
#if !defined(_WIN32)
template <size_t N>
inline int strncpy_s(char (&dst)[N], const char* src, size_t count) { return strncpy_s(dst, N, src, count); }
template <size_t N>
inline int strcpy_s(char (&dst)[N], const char* src) { return strcpy_s(dst, N, src); }
#endif
