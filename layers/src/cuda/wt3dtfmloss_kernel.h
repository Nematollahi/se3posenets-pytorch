#ifndef _WT3DTFMLOSS_KERNEL
#define _WT3DTFMLOSS_KERNEL

#ifdef __cplusplus
extern "C" {
#endif

float Weighted3DTransformLoss_ForwardLauncher(const float *points, const float *masks, const float *tfms, const float *targetpts, const float *numpts,
                                            int batchSize, int ndim, int nrows, int ncols, int nSE3, int nTfmParams,
                                            const long *ps, const long *ms, const long *ts,
                                            cudaStream_t stream);

void Weighted3DTransformLoss_BackwardLauncher(const float *points, const float *masks, const float *tfms, const float *targetpts, const float *numpts,
                                             float *gradPoints, float *gradMasks, float *gradTfms, int useMaskGradMag,
                                             int batchSize, int ndim, int nrows, int ncols, int nSE3, int nTfmParams,
                                             const long *ps, const long *ms, const long *ts,
                                             cudaStream_t stream);

#ifdef __cplusplus
}
#endif

#endif

