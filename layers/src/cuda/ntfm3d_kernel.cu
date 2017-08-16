#ifdef __cplusplus
extern "C" {
#endif

#include <stdio.h>
#include <math.h>
#include <float.h>
#include <assert.h>
#include "cudautils.h"

__constant__ float constTfms[15000];  // ... or some other big enough number

// =============== FWD PASS ================== //

///////////// Kernel
// Compute the transformed points by transforming each input point by all the "k" transforms, weighting the results
// by the mask values and summing the resulting weighted points (in parallel)
__global__ void computeTransformedPoints(const float *points, const float *masks, float *tfmpoints,
                                         int nrows, int ncols, int npoints, int nSE3,
                                         int ps0, int ps1, int ps2, int ps3,
                                         int ms0, int ms1, int ms2, int ms3,
                                         int ts0, int ts1, int ts2, int ts3)
{
    // Get the index of the point
    int id = blockIdx.x * blockDim.x + threadIdx.x; // Since they are 1D only
    if (id >= npoints) return;

    // Get the batch, row and column indices
    int b,r,c;
    getCoordinates(id, nrows, ncols, b, r, c);

    // Get 3D input point (p)
    int valp = b*ps0 + r*ps2 + c*ps3; // Don't add stride along 3D dim
    float x = *(points + 0*ps1 + valp);
    float y = *(points + 1*ps1 + valp);
    float z = *(points + 2*ps1 + valp);

    // Compute p + sum_k w_k * (R_k*p + t_k - p) across the different SE3s
    int valm = b*ms0 + r*ms2 + c*ms3;
    float xt = x, yt = y, zt = z;
    for (int k = 0; k < nSE3; k++)
    {
        // Get transform & wt
        float w_k = *(masks + k*ms1 + valm);  // Get the weight for the 'k'th transform "
        float *T = constTfms + b*ts0 + k*ts1; // Get the 'k'th transform

        // Add w_k * (R_k*p + t_k) (for X,Y,Z coordinates)
        xt += w_k * (T[0] * x + T[1] * y + T[2]  * z + T[3]  - x); // w_k * (R_k * p_x + t_k - p_x)
        yt += w_k * (T[4] * x + T[5] * y + T[6]  * z + T[7]  - y); // w_k * (R_k * p_y + t_k - p_y)
        zt += w_k * (T[8] * x + T[9] * y + T[10] * z + T[11] - z); // w_k * (R_k * p_z + t_k - p_z)
    }

    // Copy to output
    *(tfmpoints + 0*ps1 + valp) = xt;
    *(tfmpoints + 1*ps1 + valp) = yt;
    *(tfmpoints + 2*ps1 + valp) = zt;
}

///////////////// FWD pass launcher
int NTfm3D_ForwardLauncher(const float *points, const float *masks, const float *tfms, float *tfmpoints,
								  int batchSize, int ndim, int nrows, int ncols, int nSE3, int nTfmParams,
								  const long *ps, const long *ms, const long *ts,
								  cudaStream_t stream)
{
    // Copy transforms to constant memory to reduce global memory read overhead
    cudaMemcpyToSymbol(constTfms, tfms, nTfmParams * sizeof(float));

    // Block and thread structure - we have one large set of points, so use 1d block/threads
    int npoints = batchSize * nrows * ncols;
    int numBlocks = ceil(npoints * (1.0/256));
    dim3 blocks(numBlocks);
    dim3 threads(256);

//    // Timer
//    cudaEvent_t start, stop;
//    cudaEventCreate(&start);
//    cudaEventCreate(&stop);
//    cudaEventRecord(start);

    // Project the points and run the depth test first (parallelize across number of points)
    computeTransformedPoints <<< blocks, threads, 0, stream >>>(
                                                                 points,
                                                                 masks,
                                                                 tfmpoints,
                                                                 nrows,
                                                                 ncols,
                                                                 npoints,
                                                                 nSE3,
                                                                 (int) ps[0],
                                                                 (int) ps[1],
                                                                 (int) ps[2],
                                                                 (int) ps[3],
                                                                 (int) ms[0],
                                                                 (int) ms[1],
                                                                 (int) ms[2],
                                                                 (int) ms[3],
                                                                 (int) ts[0],
                                                                 (int) ts[1],
                                                                 (int) ts[2],
                                                                 (int) ts[3]
                                                                             );

    // Wait for kernel to finish
    cudaDeviceSynchronize();

//    // Finish timing and show stats
//    cudaEventRecord(stop);
//    cudaEventSynchronize(stop);
//    float milliseconds = 0;
//    cudaEventElapsedTime(&milliseconds, start, stop);
//    printf("FWD: Time taken in milliseconds: %f\n",milliseconds);

    // check for errors
    cudaError_t err = cudaGetLastError();
    if (err != cudaSuccess) {
        printf("error in NTfm3D_ForwardLauncher: %s\n", cudaGetErrorString(err));
        assert(false);
    }

    return 1;
}

// ============= BWD PASS =================== //

// Compute the gradients w.r.t input points & masks given gradients w.r.t output 3D points
__global__ void computeGradients(const float *points, const float *masks,
                                 float *gradPoints, float *gradMasks, float *gradTfms,
                                 const float *gradTfmpoints,
                                 int nrows, int ncols, int nSE3,
                                 int ps0, int ps1, int ps2, int ps3,
                                 int ms0, int ms1, int ms2, int ms3,
                                 int ts0, int ts1, int ts2, int ts3)
{
    // Get the row, col, batch IDs & figure out if we are within limits
    int c = (blockIdx.x * blockDim.x) + threadIdx.x; // col ID (innermost dimension in our data for coalescing)
    int r = (blockIdx.y * blockDim.y) + threadIdx.y; // row ID
    int b = blockIdx.z; // Batch ID (since blockDim.z = 1, theadIdx.z = 0)
    bool withinLimits = ((c < ncols) && (r < nrows));

    // Create a shared memory buffer for storing the gradients w.r.t a single transform
    extern __shared__ float sharedData[];

    // Declare temp vars
    int tid = threadIdx.x + threadIdx.y * blockDim.x; // Id of thread in local block
    int nThreads = blockDim.x * blockDim.y;
    int nThreads2 = nThreads/2;
    int nSharedGrads  = nThreads * 12;
    int nSharedGradResults = nSE3*12;
    float *sharedGradTfms = sharedData; // nThreads*12
    float *sharedGradTfmResults = (float *)&sharedData[nSharedGrads]; // nSE3*12

    // Get 3D input point (p) & gradient w.r.t output point (gpt). Read only if inside limits
    float x, y, z, gxt, gyt, gzt;
    int valp = b*ps0 + r*ps2 + c*ps3; // Don't add stride along 3D dim
    if (withinLimits)
    {
        x = *(points + 0*ps1 + valp);
        y = *(points + 1*ps1 + valp);
        z = *(points + 2*ps1 + valp);

        // Get gradient w.r.t output point (gpt)
        gxt = *(gradTfmpoints + 0*ps1 + valp);
        gyt = *(gradTfmpoints + 1*ps1 + valp);
        gzt = *(gradTfmpoints + 2*ps1 + valp);
    }

    // Compute the gradients over all the transforms from a given 3D point
    int valm = b*ms0 + r*ms2 + c*ms3;
    float gx = gxt, gy = gyt, gz = gzt; // Grads w.r.t input pts (init to gradients w.r.t tfm points)
    for(int k = 0; k < nSE3; k++)
    {
        // Compute all the gradients if within limits or set the grads to zero
        if(withinLimits)
        {
            // Get transform & wt
            float w_k = *(masks + k*ms1 + valm);   // Get the weight for the 'k'th transform "
            float *T  = constTfms + b*ts0 + k*ts1; // Get the 'k'th transform

            // Create temp scalars
            float tx = (T[0] * gxt + T[4] * gyt + T[8]  * gzt);
            float ty = (T[1] * gxt + T[5] * gyt + T[9]  * gzt);
            float tz = (T[2] * gxt + T[6] * gyt + T[10] * gzt);

            // === Gradient w.r.t input point (p = R^T * gpt - gpt, summed across all the "k" transforms)
            gx += w_k * (tx - gxt);
            gy += w_k * (ty - gyt);
            gz += w_k * (tz - gzt);

            // === Gradient w.r.t mask (w_k) = (R_k^T * p + t_k - p) * gpt
            *(gradMasks + k*ms1 + valm) = x * tx + y * ty + z * tz +
                                          gxt * (T[3] - x) + gyt * (T[7] - y) + gzt * (T[11] - z);

            // === Gradients w.r.t transforms (t_k), stored in shared memory
            // Grads w.r.t rotation parameters (sum across all pts)
            // First nThreads params is Tfm(0,0), next is Tfm(0,1) etc for removing memory bank conflicts when reading to shared memory
            sharedGradTfms[0*nThreads+tid]  = w_k * x * gxt;
            sharedGradTfms[1*nThreads+tid]  = w_k * y * gxt;
            sharedGradTfms[2*nThreads+tid]  = w_k * z * gxt;
            sharedGradTfms[4*nThreads+tid]  = w_k * x * gyt;
            sharedGradTfms[5*nThreads+tid]  = w_k * y * gyt;
            sharedGradTfms[6*nThreads+tid]  = w_k * z * gyt;
            sharedGradTfms[8*nThreads+tid]  = w_k * x * gzt;
            sharedGradTfms[9*nThreads+tid]  = w_k * y * gzt;
            sharedGradTfms[10*nThreads+tid] = w_k * z * gzt;

            // Grads w.r.t translation parameters (sum across all pts)
            sharedGradTfms[3*nThreads+tid]  = w_k * gxt;
            sharedGradTfms[7*nThreads+tid]  = w_k * gyt;
            sharedGradTfms[11*nThreads+tid] = w_k * gzt;
        }
        else
        {
            // Re-initialize shared memory to zero (no need to sync here as we don't += to this memory till we do a syncthreads later)
            for(int i = tid; i < nSharedGrads; i+=nThreads)
                sharedGradTfms[i] = 0;
        }
        __syncthreads(); // Synchronize all threads before we sum up the tfm gradients

        // === Do the parallel reduce for that particular transform dimension
        // === ASSUMPTION: We have power of 2 block sizes!
        // From: Slide 22 of http://developer.download.nvidia.com/compute/cuda/1.1-Beta/x86_website/projects/reduction/doc/reduction.pdf
        // We use first half of threads to compute sums for first 6 transform params & the rest for the last 6 params
        for(unsigned int s = nThreads2; s>=32; s>>=1)
        {
            // Second nThreads/2 elements will be added to first nThreads/2 elements, then
            // Second nThreads/4 elements will be added to first nThreads/4 elements and so on!
            if (tid < s)
            {
                // Sum up gradients w.r.t first 6 parameters!
                for(int i = 0; i < 6; i++)
                    sharedGradTfms[i*nThreads + tid] += sharedGradTfms[i*nThreads + tid + s];
            }
            else if((tid >= nThreads2) && (tid - nThreads2) < s) // Use the second half of threads to process the remaining 6 transform parameters
            {
                // Sum up gradients w.r.t last 6 parameters!
                for(int i = 6; i < 12; i++)
                    sharedGradTfms[i*nThreads + tid - nThreads2] += sharedGradTfms[i*nThreads + tid - nThreads2 + s];
            }
            __syncthreads();
        }

        // This uses warp-shuffle to compute the sum across a warp (32 threads)
        // Note that for this to work, you have to have run the loop until the sum is computed for the first 32 threads in the warp
        if (tid < 32)
        {
            for(int i = 0; i < 12; i++)
            {
                float sum = warpReduceSum(sharedGradTfms[i*nThreads + tid]);
                if (tid == 0)
                    sharedGradTfmResults[k*12+i] = sum; // Store final summed result in shared memory, we can copy to global later in parallel
            }
        }
    }
    __syncthreads(); // Wait till all gradients have been propely summed up!

    // Add computed tfm gradients to global memory in parallel!
    for(int i = tid; i < nSharedGradResults; i+=nThreads)
        atomicAdd(gradTfms + b*ts0 + i, sharedGradTfmResults[i]); // Final value corresponding to that term of the tfm

    // Gradients w.r.t pts (copy after sum across tfms)
    if (withinLimits)
    {
        *(gradPoints + 0*ps1 + valp) = gx;
        *(gradPoints + 1*ps1 + valp) = gy;
        *(gradPoints + 2*ps1 + valp) = gz;
    }
}

////////////////////////////////////
// == BWD pass code
int NTfm3D_BackwardLauncher(const float *points, const float *masks, const float *tfms, const float *tfmpoints,
									float *gradPoints, float *gradMasks, float *gradTfms, const float *gradTfmpoints,
									int batchSize, int ndim, int nrows, int ncols, int nSE3, int nTfmParams,
									const long *ps, const long *ms, const long *ts,
									cudaStream_t stream)
{
    // Copy transforms to constant memory to reduce global memory read overhead
    cudaMemcpyToSymbol(constTfms, tfms, nTfmParams * sizeof(float));

    // Compute gradients w.r.t the input tfms next
    dim3 threads(16,16,1);
    dim3 blocks(ceil(ncols*(1.0/threads.x)),ceil(nrows*(1.0/threads.y)),batchSize); // all threads in a block will access same example
    int sharedMemSize = threads.x * threads.y * 3 * 4 * sizeof(float) + nSE3 * 3 * 4 * sizeof(float); // Memory for 12 vals per thread + nSE3*12 vals for storing result
    if (sharedMemSize > 32000)
    {
        printf("Shared memory size for transform gradients (%d) > 32000. Can't be stored in shared memory."
               "Please reduce number of threads per block \n", sharedMemSize);
        assert(false); // Exit
    }

//    // Timer
//    cudaEvent_t start, stop;
//    cudaEventCreate(&start);
//    cudaEventCreate(&stop);
//    cudaEventRecord(start);

    computeGradients<<< blocks, threads, sharedMemSize, stream >>>(
                                                                    points,
                                                                    masks,
                                                                    gradPoints,
                                                                    gradMasks,
                                                                    gradTfms,
                                                                    gradTfmpoints,
                                                                    nrows,
                                                                    ncols,
                                                                    nSE3,
                                                                    (int) ps[0],
                                                                    (int) ps[1],
                                                                    (int) ps[2],
                                                                    (int) ps[3],
                                                                    (int) ms[0],
                                                                    (int) ms[1],
                                                                    (int) ms[2],
                                                                    (int) ms[3],
                                                                    (int) ts[0],
                                                                    (int) ts[1],
                                                                    (int) ts[2],
                                                                    (int) ts[3]
                                                                                );

    // Wait for kernel to finish
    cudaDeviceSynchronize();

//    // Finish timing and show stats
//    cudaEventRecord(stop);
//    cudaEventSynchronize(stop);
//    float milliseconds = 0;
//    cudaEventElapsedTime(&milliseconds, start, stop);
//    printf("BWD: Time taken in milliseconds: %f\n",milliseconds);

    // check for errors
    cudaError_t err = cudaGetLastError();
    if (err != cudaSuccess) {
        printf("error in NTfm3D_BackwardLauncher: %s\n", cudaGetErrorString(err));
        assert(false);
    }

    return 1;
}

#ifdef __cplusplus
}
#endif
