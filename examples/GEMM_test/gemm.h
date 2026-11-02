#ifndef GEMM_H
#define GEMM_H

// Largest N the on-chip accumulator row can hold. M and K are only limited by DDR.
#define GEMM_MAX_N 1024

// C[M][N] = A[M][K] * B[K][N], float32, row-major, all three in DDR.
void gemm(const float *A, const float *B, float *C, int M, int N, int K);

#endif
