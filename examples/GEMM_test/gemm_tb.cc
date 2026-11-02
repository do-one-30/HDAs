// C simulation test: random A, B for several shapes, compared with a double-precision reference.
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <vector>

#include "gemm.h"

static float rand_unit(unsigned &state) {
    state = state * 1664525u + 1013904223u;
    return ((state >> 8) & 0xFFFF) / 32768.0f - 1.0f;  // [-1, 1)
}

static int check(int M, int N, int K, unsigned seed) {
    std::vector<float> A(M * K), B(K * N), C(M * N, 0.0f);
    for (auto &x : A) x = rand_unit(seed);
    for (auto &x : B) x = rand_unit(seed);
    gemm(A.data(), B.data(), C.data(), M, N, K);

    int bad = 0;
    double max_err = 0.0;
    for (int i = 0; i < M; i++)
        for (int j = 0; j < N; j++) {
            double ref = 0.0;
            for (int k = 0; k < K; k++) ref += (double)A[i * K + k] * B[k * N + j];
            double err = std::fabs(C[i * N + j] - ref);
            max_err = err > max_err ? err : max_err;
            if (err > 1e-4 * K + 1e-5 * std::fabs(ref)) bad++;
        }
    printf("M=%-4d N=%-4d K=%-4d max_err=%.3e bad=%d -> %s\n", M, N, K, max_err, bad, bad ? "FAIL" : "PASS");
    return bad;
}

int main() {
    // shapes: 1x1, square, odd sizes, N not equal to K
    const int shapes[][3] = {{1, 1, 1}, {16, 16, 16}, {37, 19, 23}, {64, 128, 32}};
    int fails = 0;
    for (const auto &s : shapes) fails += check(s[0], s[1], s[2], 1234u + s[0]) != 0;
    printf(fails ? "TEST FAILED\n" : "TEST PASSED\n");
    return fails ? 1 : 0;
}
