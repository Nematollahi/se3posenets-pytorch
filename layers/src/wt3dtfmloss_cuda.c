#include <THC/THC.h>
#include <math.h>
#include <assert.h>
#include <stdbool.h>
#include "cuda/wt3dtfmloss_kernel.h"

extern THCState *state;

// =============== FWD PASS ================== //
int Weighted3DTransformLoss_forward_cuda(
			THCudaTensor *points,
			THCudaTensor *masks,
			THCudaTensor *tfms,
			THCudaTensor *targetpoints,
			int sizeAverage)
{
    // Initialize vars
    int batchSize = points->size[0];
    int ndim      = points->size[1];
    int nrows     = points->size[2];
    int ncols     = points->size[3];
    int nSE3      = masks->size[1];
    assert(ndim == 3); // 3D points

    // Check if we can fit all transforms within constant memory
    int nTfmParams = THCudaTensor_nElement(state, tfms);
    if (nTfmParams > 15000)
    {
        printf("Number of transform parameters (%d) > 15000. Can't be stored in constant memory."
               "Please use NonRigidTransform3D layer + MSE criterion instead \n", nTfmParams);
        assert(false); // Exit
    }

    // New memory in case the inputs are not contiguous
    points = THCudaTensor_newContiguous(state, points);
    masks  = THCudaTensor_newContiguous(state, masks);
    tfms   = THCudaTensor_newContiguous(state, tfms);
    targetpoints = THCudaTensor_newContiguous(state, targetpoints);

    // Get data pointers
    float *points_data    = THCudaTensor_data(state, points);
    float *masks_data 	  = THCudaTensor_data(state, masks);
    float *tfms_data      = THCudaTensor_data(state, tfms);
    float *targetpts_data = THCudaTensor_data(state, targetpoints);

    // Get strides
    long *ps = points->stride;
    long *ms = masks->stride;
    long *ts = tfms->stride;

	// Get current cuda stream
	cudaStream_t stream = THCState_getCurrentStream(state);

	// Run the kernel
    float loss = Weighted3DTransformLoss_ForwardLauncher(
                      points_data, masks_data, tfms_data, targetpts_data,
                      batchSize, ndim, nrows, ncols, nSE3, nTfmParams,
                      ps, ms, ts,
                      stream);

    // Divide by number of points if asked for average
    loss *= 0.5; // Scale loss by 0.5
    if(sizeAverage)
    {
        long nElements = THCudaTensor_nElement(state, points);
        loss /= ((float)nElements);
    }

    // Free memory
    THCudaTensor_free(state, points);
    THCudaTensor_free(state, masks);
    THCudaTensor_free(state, tfms);
    THCudaTensor_free(state, targetpoints);

    return loss;
}

// =============== BWD PASS ================== //
void Weighted3DTransformLoss_backward_cuda(
			THCudaTensor *points,
			THCudaTensor *masks,
			THCudaTensor *tfms,
			THCudaTensor *targetpoints,
			THCudaTensor *gradPoints,
			THCudaTensor *gradMasks,
			THCudaTensor *gradTfms,
            THCudaTensor *gradOutput,
			int sizeAverage)
{
    // Initialize vars
    long batchSize = points->size[0];
    long ndim      = points->size[1];
    long nrows     = points->size[2];
    long ncols     = points->size[3];
    long nSE3      = masks->size[1];
    assert(ndim == 3); // 3D points

    // Check if we can fit all transforms within constant memory
    int nTfmParams = THCudaTensor_nElement(state, tfms);
    if (nTfmParams > 15000)
    {
        printf("Number of transform parameters (%d) > 15000. Can't be stored in constant memory."
               "Please use NonRigidTransform3D layer + MSE criterion instead \n", nTfmParams);
        assert(false); // Exit
    }

	 // Set gradients w.r.t pts & tfms to zero (as we add to these in a loop later)
    THCudaTensor_fill(state, gradPoints, 0);
    THCudaTensor_fill(state, gradTfms, 0);

    // New memory in case the inputs are not contiguous
    points = THCudaTensor_newContiguous(state, points);
    masks  = THCudaTensor_newContiguous(state, masks);
    tfms   = THCudaTensor_newContiguous(state, tfms);
    targetpoints = THCudaTensor_newContiguous(state, targetpoints);

    // Get data pointers
    float *points_data        = THCudaTensor_data(state, points);
    float *masks_data         = THCudaTensor_data(state, masks);
    float *tfms_data 		  = THCudaTensor_data(state, tfms);
    float *targetpts_data 	  = THCudaTensor_data(state, targetpoints);
    float *gradPoints_data    = THCudaTensor_data(state, gradPoints);
    float *gradMasks_data 	  = THCudaTensor_data(state, gradMasks);
    float *gradTfms_data      = THCudaTensor_data(state, gradTfms);
    float *gradOutput_data    = THCudaTensor_data(state, gradOutput);

	 // Get strides
    long *ps = points->stride;
    long *ms = masks->stride;
    long *ts = tfms->stride;

	// Get current cuda stream
	cudaStream_t stream = THCState_getCurrentStream(state);

	// Run the kernel
    Weighted3DTransformLoss_BackwardLauncher(
		  points_data, masks_data, tfms_data, targetpts_data,
		  gradPoints_data, gradMasks_data, gradTfms_data,
		  batchSize, ndim, nrows, ncols, nSE3, nTfmParams,
		  ps, ms, ts,
		  stream);

    // Get gradient w.r.t output
    float norm;
    cudaMemcpy(&norm, gradOutput_data, sizeof(float), cudaMemcpyDeviceToHost);
    if (sizeAverage)
    {
        // Average the gradients if "sizeAverage" is set
        long nElements = THCudaTensor_nElement(state, points);
        norm *= 1.0/((float)nElements);
    }

    // Scale by grad output & average gradients
    THCudaTensor_mul(state, gradPoints, gradPoints, norm);
    THCudaTensor_mul(state, gradMasks, gradMasks, norm);
    THCudaTensor_mul(state, gradTfms, gradTfms, norm);

    // Free memory
    THCudaTensor_free(state, points);
    THCudaTensor_free(state, masks);
    THCudaTensor_free(state, tfms);
    THCudaTensor_free(state, targetpoints);
}