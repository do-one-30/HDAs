#include "gemm.h"

// Simple reference GEMM for the measurement flow.
// One output row at a time: acc[] holds row i of C, A[i][k] is read once per k and
// row k of B is streamed with a burst, so the inner j loop runs at II=1.
// B is re-read for every row of C, so the kernel is memory bound: about M*K*N cycles.
void gemm(const float *A, const float *B, float *C, int M, int N, int K) {
#pragma HLS INTERFACE m_axi port=A offset=slave bundle=gmem0 depth=4096 max_read_burst_length=256
#pragma HLS INTERFACE m_axi port=B offset=slave bundle=gmem1 depth=4096 max_read_burst_length=256
#pragma HLS INTERFACE m_axi port=C offset=slave bundle=gmem2 depth=8192 max_write_burst_length=256
#pragma HLS INTERFACE s_axilite port=M
#pragma HLS INTERFACE s_axilite port=N
#pragma HLS INTERFACE s_axilite port=K
#pragma HLS INTERFACE s_axilite port=return

    float acc[GEMM_MAX_N];

rows:
    for (int i = 0; i < M; i++) {
#pragma HLS LOOP_TRIPCOUNT min=1 max=256
    clear:
        for (int j = 0; j < N; j++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=1 max=256
            acc[j] = 0.0f;
        }
    kdim:
        for (int k = 0; k < K; k++) {
#pragma HLS LOOP_TRIPCOUNT min=1 max=256
            // keep the j pipeline flushed between k steps: acc[j] is reused at k+1
#pragma HLS LOOP_FLATTEN off
            float a = A[i * K + k];
        cols:
            for (int j = 0; j < N; j++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=1 max=256
                acc[j] += a * B[k * N + j];
            }
        }
    store:
        for (int j = 0; j < N; j++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=1 max=256
            C[i * N + j] = acc[j];
        }
    }
}
