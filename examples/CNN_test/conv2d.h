#ifndef CONV2D_H
#define CONV2D_H

// On-chip buffer limits. Feature maps themselves live in DDR.
#define CONV_MAX_W     224          // input width W and output width W_out
#define CONV_MAX_WGT   (512 * 3 * 3) // C_in * K * K of one output channel

// out[co][oh][ow] = relu?( bias[co] + sum_{ci,kh,kw} wgt[co][ci][kh][kw] * in[ci][oh*s-p+kh][ow*s-p+kw] )
// float32, NCHW with batch 1, zero padding. H_out = (H + 2p - K) / s + 1, same for W_out.
void conv2d(const float *in, const float *wgt, const float *bias, float *out,
            int C_in, int C_out, int H, int W, int K, int stride, int pad, int relu);

#endif
