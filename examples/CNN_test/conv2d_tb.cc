// C simulation test: random input/weights/bias for several layer shapes,
// compared with a double-precision reference (zero padding, optional ReLU).
#include <cmath>
#include <cstdio>
#include <vector>

#include "conv2d.h"

static float rand_unit(unsigned &state) {
    state = state * 1664525u + 1013904223u;
    return ((state >> 8) & 0xFFFF) / 32768.0f - 1.0f;  // [-1, 1)
}

struct Shape { int C_in, C_out, H, W, K, stride, pad, relu; };

static int check(const Shape &s, unsigned seed) {
    const int H_out = (s.H + 2 * s.pad - s.K) / s.stride + 1;
    const int W_out = (s.W + 2 * s.pad - s.K) / s.stride + 1;
    std::vector<float> in(s.C_in * s.H * s.W), wgt(s.C_out * s.C_in * s.K * s.K), bias(s.C_out);
    std::vector<float> out(s.C_out * H_out * W_out, NAN);
    for (auto &x : in) x = rand_unit(seed);
    for (auto &x : wgt) x = rand_unit(seed);
    for (auto &x : bias) x = rand_unit(seed);
    conv2d(in.data(), wgt.data(), bias.data(), out.data(),
           s.C_in, s.C_out, s.H, s.W, s.K, s.stride, s.pad, s.relu);

    const int n_terms = s.C_in * s.K * s.K;
    int bad = 0;
    double max_err = 0.0;
    for (int co = 0; co < s.C_out; co++)
        for (int oh = 0; oh < H_out; oh++)
            for (int ow = 0; ow < W_out; ow++) {
                double ref = bias[co];
                for (int ci = 0; ci < s.C_in; ci++)
                    for (int kh = 0; kh < s.K; kh++)
                        for (int kw = 0; kw < s.K; kw++) {
                            int ih = oh * s.stride - s.pad + kh, iw = ow * s.stride - s.pad + kw;
                            if (ih < 0 || ih >= s.H || iw < 0 || iw >= s.W) continue;
                            ref += (double)wgt[((co * s.C_in + ci) * s.K + kh) * s.K + kw] *
                                   in[(ci * s.H + ih) * s.W + iw];
                        }
                if (s.relu && ref < 0) ref = 0;
                double got = out[(co * H_out + oh) * W_out + ow];
                double err = std::fabs(got - ref);
                if (!(err <= 2e-6 * n_terms + 1e-5 * std::fabs(ref))) bad++;   // also catches NaN
                if (err > max_err) max_err = err;
            }
    printf("Cin=%-3d Cout=%-3d %3dx%-3d K=%d s=%d p=%d relu=%d -> %dx%d  max_err=%.3e bad=%d -> %s\n",
           s.C_in, s.C_out, s.H, s.W, s.K, s.stride, s.pad, s.relu, H_out, W_out, max_err, bad,
           bad ? "FAIL" : "PASS");
    return bad;
}

int main() {
    // 1x1 conv, 3x3 same, 3x3 stride 2, 5x5 non-square, 7x7 stride 2 (ResNet conv1 style)
    const Shape shapes[] = {
        {1, 1, 5, 5, 1, 1, 0, 0},
        {4, 3, 8, 8, 3, 1, 1, 0},
        {3, 4, 9, 11, 3, 2, 1, 1},
        {2, 2, 13, 7, 5, 2, 2, 1},
        {3, 8, 16, 16, 7, 2, 3, 1},
    };
    int fails = 0;
    unsigned seed = 1;
    for (const auto &s : shapes) fails += check(s, seed++ * 7919u) != 0;
    printf(fails ? "TEST FAILED\n" : "TEST PASSED\n");
    return fails ? 1 : 0;
}
