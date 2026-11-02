#include "conv2d.h"

// Simple reference convolution for the measurement flow.
// Per output channel: its weights (C_in*K*K) and bias go on chip once.
// Per output row: acc[] holds the row; for every (ci, kh) one input row is burst-read
// into row[], then for every kw the ow loop accumulates at II=1 (one MAC per cycle).
// Cycles ~ C_out * H_out * C_in * K * (W + K * W_out).
void conv2d(const float *in, const float *wgt, const float *bias, float *out,
            int C_in, int C_out, int H, int W, int K, int stride, int pad, int relu) {
#pragma HLS INTERFACE m_axi port=in   offset=slave bundle=gmem0 depth=1024 max_read_burst_length=256
#pragma HLS INTERFACE m_axi port=wgt  offset=slave bundle=gmem1 depth=1024 max_read_burst_length=256
#pragma HLS INTERFACE m_axi port=bias offset=slave bundle=gmem2 depth=64
#pragma HLS INTERFACE m_axi port=out  offset=slave bundle=gmem3 depth=2048 max_write_burst_length=256
#pragma HLS INTERFACE s_axilite port=C_in
#pragma HLS INTERFACE s_axilite port=C_out
#pragma HLS INTERFACE s_axilite port=H
#pragma HLS INTERFACE s_axilite port=W
#pragma HLS INTERFACE s_axilite port=K
#pragma HLS INTERFACE s_axilite port=stride
#pragma HLS INTERFACE s_axilite port=pad
#pragma HLS INTERFACE s_axilite port=relu
#pragma HLS INTERFACE s_axilite port=return

    float wbuf[CONV_MAX_WGT];
    float row[CONV_MAX_W];
    float acc[CONV_MAX_W];

    const int H_out = (H + 2 * pad - K) / stride + 1;
    const int W_out = (W + 2 * pad - K) / stride + 1;
    const int n_wgt = C_in * K * K;

out_ch:
    for (int co = 0; co < C_out; co++) {
#pragma HLS LOOP_TRIPCOUNT min=1 max=256
    load_w:
        for (int i = 0; i < n_wgt; i++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=1 max=4608
            wbuf[i] = wgt[co * n_wgt + i];
        }
        const float b = bias[co];

    out_row:
        for (int oh = 0; oh < H_out; oh++) {
#pragma HLS LOOP_TRIPCOUNT min=1 max=112
        clear:
            for (int ow = 0; ow < W_out; ow++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=1 max=112
                acc[ow] = b;
            }
        in_ch:
            for (int ci = 0; ci < C_in; ci++) {
#pragma HLS LOOP_TRIPCOUNT min=1 max=256
            k_row:
                for (int kh = 0; kh < K; kh++) {
#pragma HLS LOOP_TRIPCOUNT min=1 max=7
                    const int ih = oh * stride - pad + kh;
                    if (ih < 0 || ih >= H) continue;   // zero padding: row contributes nothing
                load_row:
                    for (int iw = 0; iw < W; iw++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=1 max=224
                        row[iw] = in[(ci * H + ih) * W + iw];
                    }
                k_col:
                    for (int kw = 0; kw < K; kw++) {
#pragma HLS LOOP_TRIPCOUNT min=1 max=7
                        // keep the ow pipeline flushed between kw steps: acc[ow] is reused at kw+1
#pragma HLS LOOP_FLATTEN off
                        const float w = wbuf[(ci * K + kh) * K + kw];
                    mac:
                        for (int ow = 0; ow < W_out; ow++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=1 max=112
                            const int iw = ow * stride - pad + kw;
                            if (iw >= 0 && iw < W) acc[ow] += w * row[iw];
                        }
                    }
                }
            }
        store:
            for (int ow = 0; ow < W_out; ow++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=1 max=112
                const float v = acc[ow];
                out[(co * H_out + oh) * W_out + ow] = (relu && v < 0.0f) ? 0.0f : v;
            }
        }
    }
}
