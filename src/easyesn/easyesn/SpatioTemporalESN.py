"""
    Implementation of the general ESN model.
"""

import numpy as np
import numpy.random as rnd
from .BaseESN import BaseESN

from . import backend as B

from sklearn.linear_model import Ridge
from sklearn.svm import SVR
from sklearn.linear_model import LogisticRegression
import progressbar

#import dill

from multiprocess import Process, Queue, Manager, Pool, cpu_count #we require Pathos version >=0.2.6. Otherwise we will get an "EOFError: Ran out of input" exception
#import multiprocessing
import ctypes
from multiprocessing import process

class SpatioTemporalESN(BaseESN):
    def __init__(self, inputShape, n_reservoir,
                 filterSize=1, stride=1, borderMode="mirror", nWorkers="auto",
                 spectralRadius=1.0, noiseLevel=0.0, inputScaling=None,
                 leakingRate=1.0, sparseness=0.2, random_seed=None, averageOutputWeights=True,
                 out_activation=lambda x: x, out_inverse_activation=lambda x: x,
                 weight_generation='naive', bias=1.0, output_bias=1.0,
                 outputInputScaling=1.0, input_density=1.0, solver='pinv', regression_parameters={}, activation = B.tanh):

        self._averageOutputWeights = averageOutputWeights
        if averageOutputWeights and solver != "lsqr":
            raise ValueError("`averageOutputWeights` can only be set to `True` when `solver` is set to `lsqr` (Ridge Regression)")

        self._borderMode = borderMode
        if not borderMode in ["mirror", "padding", "edge", "wrap"]:
            raise ValueError("`borderMode` must be set to one of the following values: `mirror`, `padding`, `edge` or `wrap`.")

        self._regression_parameters = regression_parameters
        self._solver = solver

        n_inputDimensions = len(inputShape)

        if filterSize % 2 == 0:
            raise ValueError("filterSize has to be an odd number (1, 3, 5, ...).")
        self._filterSize = filterSize
        self._filterWidth = int(np.floor(filterSize / 2))
        self._stride = stride

        self._n_input = int(np.power(np.ceil(filterSize / stride), n_inputDimensions))

        self.n_inputDimensions = n_inputDimensions
        self.inputShape = inputShape

        if not self._averageOutputWeights:
            self._WOuts = B.empty((np.prod(inputShape), 1, self._n_input+n_reservoir+1))
        else:
            self._WOuts = None
            self._WOut = B.zeros((1, self._n_input+n_reservoir+1))
        self._xs = B.empty((np.prod(inputShape), n_reservoir, 1))

        
        if nWorkers=="auto":
            self._nWorkers = np.max((cpu_count()-1, 1))
        else:
            self._nWorkers = nWorkers
            

        manager = Manager()
        self.sharedNamespace = manager.Namespace()
        if hasattr(self, "fitWorkerID") == False or self.parallelWorkerIDs is None:
            self.parallelWorkerIDs = manager.Queue()
            for i in range(self._nWorkers):
                self.parallelWorkerIDs.put((i))

        super(SpatioTemporalESN, self).__init__(n_input=self._n_input, n_reservoir=n_reservoir, n_output=1, spectralRadius=spectralRadius,
                                  noiseLevel=noiseLevel, inputScaling=inputScaling, leakingRate=leakingRate, sparseness=sparseness,
                                  random_seed=random_seed, out_activation=out_activation, out_inverse_activation=out_inverse_activation,
                                  weight_generation=weight_generation, bias=bias, output_bias=output_bias, outputInputScaling=outputInputScaling,
                                  input_density=input_density, activation=activation)

        """
            allowed values for the solver:
                pinv
                lsqr (will only be used in the thesis)

                sklearn_auto
                sklearn_svd
                sklearn_cholesky
                sklearn_lsqr
                sklearn_sag
        """  
    
    def resetState(self, index=None):
        if index is None:
             self._x = B.zeros((self._nWorkers, self.n_reservoir, 1))
        else:
            self._x[index] = B.zeros((self.n_reservoir, 1))

    """
        Fits the ESN so that by applying a time series out of inputData the outputData will be produced.
    """
    def fit(self, inputData, outputData, transientTime=0, verbose=0):
        rank = len(inputData.shape) - 1

        if rank != self.n_inputDimensions:
            raise ValueError("The `inputData` does not have a suitable shape. It has to have {0} spatial dimensions and 1 temporal dimension.".format(self.n_inputDimensions))

        manager = Manager()
        fitQueue = manager.Queue()

        if self._borderMode == "mirror":
            modifiedInputData = np.pad(inputData, tuple([(0,0)] + [(self._filterWidth, self._filterWidth)]*rank), mode="symmetric")
        elif self._borderMode == "padding":
            modifiedInputData = np.pad(inputData, tuple([(0,0)] + [(self._filterWidth, self._filterWidth)]*rank), mode="constant", constant_values=0)
        elif self._borderMode == "edge":
            modifiedInputData = np.pad(inputData, tuple([(0,0)] + [(self._filterWidth, self._filterWidth)]*rank), mode="edge")
        elif self._borderMode == "wrap":
            modifiedInputData = np.pad(inputData, tuple([(0,0)] + [(self._filterWidth, self._filterWidth)]*rank), mode="wrap")

        self.sharedNamespace.inputData = modifiedInputData
        self.sharedNamespace.outputData = outputData
        self.sharedNamespace.transientTime = transientTime

        self.sharedNamespace.WOut = self._WOut
        self.sharedNamespace.WOuts = self._WOuts
        self.sharedNamespace.xs = self._xs
               
        jobs = np.stack(np.meshgrid(*[np.arange(x)+self._filterWidth for x in inputData.shape[1:]]), axis=rank).reshape(-1, rank).tolist()
        nJobs = len(jobs)

        self.sharedNamespace.WOuts = self._WOuts

        self.resetState()
        
        pool = Pool(processes=self._nWorkers, initializer=SpatioTemporalESN._init_fitProcess, initargs=[fitQueue, self])  
        processProcessResultsThread = Process(target=self._processPoolWorkerResults, args=(nJobs, fitQueue, verbose))
        processProcessResultsThread.start()

        results = pool.map(self._fitProcess, jobs)
        pool.close()

        self._WOut = self.sharedNamespace.WOut
        self._WOuts = self.sharedNamespace.WOuts
        self._xs = self.sharedNamespace.xs

    """
        Use the ESN in the predictive mode to predict the output signal by using an input signal.
    """
    def predict(self, inputData, transientTime=0, update_processor=lambda x:x, verbose=0):
        rank = len(inputData.shape) - 1

        if rank != self.n_inputDimensions:
            raise ValueError("The `inputData` does not have a suitable shape. It has to have {0} spatial dimensions and 1 temporal dimension.".format(self.n_inputDimensions))

        manager = Manager()
        predictQueue = manager.Queue()

        if self._borderMode == "mirror":
            modifiedInputData = np.pad(inputData, tuple([(0,0)] + [(self._filterWidth, self._filterWidth)]*rank), mode="symmetric")
        elif self._borderMode == "padding":
            modifiedInputData = np.pad(inputData, tuple([(0,0)] + [(self._filterWidth, self._filterWidth)]*rank), mode="constant", constant_values=0)
        elif self._borderMode == "edge":
            modifiedInputData = np.pad(inputData, tuple([(0,0)] + [(self._filterWidth, self._filterWidth)]*rank), mode="edge")
        elif self._borderMode == "wrap":
            modifiedInputData = np.pad(inputData, tuple([(0,0)] + [(self._filterWidth, self._filterWidth)]*rank), mode="wrap")

        self.sharedNamespace.inputData = modifiedInputData
        self.sharedNamespace.transientTime = transientTime
        _predictionOutput = B.zeros(np.insert(self.inputShape, 0, inputData.shape[0]-transientTime))
        self.sharedNamespace.predictionOutput = _predictionOutput
       
        jobs = np.stack(np.meshgrid(*[np.arange(x)+self._filterWidth for x in inputData.shape[1:]]), axis=rank).reshape(-1, rank).tolist()
        nJobs = len(jobs)

        #sharedNamespace.parallelWorkerIDs = list(range(self._nWorkers))

        self.resetState()

        pool = Pool(processes=self._nWorkers, initializer=SpatioTemporalESN._init_predictProcess, initargs=[predictQueue, self])  

        processProcessResultsThread = Process(target=self._processPoolWorkerResults, args=(nJobs, predictQueue, verbose))
        processProcessResultsThread.start()

        results = pool.map(self._predictProcess, jobs)
        pool.close()

        return self.sharedNamespace.predictionOutput
    
    def _uniqueIDFromIndices(self, indices):
        id = indices[-1]

        if len(indices) != len(self.inputShape):
            raise ValueError("Shape if `indices` does not match the `inputShape` of the SpatioTemporalESN.")

        if len(self.inputShape) > 1:
            for i in range(len(self.inputShape)-2, 0, -1):
                id += self.inputShape[i]*indices[i+1]
        return id

    def _processPoolWorkerResults(self, nJobs, queue, verbose):
        nJobsDone = 0
        
        if verbose > 0:
            bar = progressbar.ProgressBar(max_value=nJobs, redirect_stdout=True, poll_interval=0.0001)
            bar.update(0)

        while nJobsDone < nJobs:
            data = queue.get()
            
            if len(data) > 2:
                #result of fitting

                indices, x, WOut = data
                id = self._uniqueIDFromIndices(indices)

                 #store WOut
                if self._averageOutputWeights:
                    self.sharedNamespace.WOut += WOut/np.prod(self.inputShape)
                else:
                    self.sharedNamespace.WOuts[id] = WOut 

                #store x
                self.sharedNamespace.xs[id] = x
            else:
                #result of predicting
                indices, prediction = data
               
                #update the value in this way to submit the changes through pathos
                predictionOutput = self.sharedNamespace.predictionOutput
                predictionOutput[tuple([Ellipsis] +  indices)] = prediction
                self.sharedNamespace.predictionOutput = predictionOutput
           
            nJobsDone += 1
            if verbose > 0:
                bar.update(nJobsDone)
                if verbose > 1:
                    print(nJobsDone)
        
        if verbose > 0:
            bar.finish()

    @staticmethod
    def _init_fitProcess(fitQueue, self):
        SpatioTemporalESN._fitProcess.fitQueue = fitQueue
        SpatioTemporalESN._fitProcess.self = self

    def _fitProcess(self, indices):
        inputData = self.sharedNamespace.inputData
        outputData = self.sharedNamespace.outputData
        transientTime = self.sharedNamespace.transientTime
       
        y, x = indices

        #print(id(SpatioTemporalESN._fitProcess.sharedNamespace.parallelWorkerIDs))
        workerID = self.parallelWorkerIDs.get()
        #self.sharedNamespace.parallelWorkerIDs = self.sharedNamespace.parallelWorkerIDs

        #create patchedInputData

        #treat the frame pixels in a special way
        inData = inputData[:, y-self._filterWidth : y+self._filterWidth+1, x-self._filterWidth : x+self._filterWidth+1][:, ::self._stride, ::self._stride].reshape(len(inputData), -1)
        #create target output series
        outData = outputData[:, y-self._filterWidth, x-self._filterWidth].reshape(-1, 1)

        #now fit
        X = self.propagate(inData, transientTime, x=self._x[workerID], verbose=0)

        #define the target values
        Y_target = self.out_inverse_activation(outData).T[:, transientTime:]

        X_T = X.T
        WOut = B.dot(B.dot(Y_target, X_T),B.inv(B.dot(X, X_T) + self._regression_parameters[0]*B.identity(1+self.n_input+self.n_reservoir)))
        
        #calculate the training prediction now
        trainingPrediction = self.out_activation(B.dot(WOut, X).T)
            
        #store the state and the output matrix of the worker
        SpatioTemporalESN._fitProcess.fitQueue.put(([x-self._filterWidth for x in indices], self._x[workerID].copy(), WOut.copy()))

        self.parallelWorkerIDs.put(workerID)

    @staticmethod
    def _init_predictProcess(predictQueue, self):
        SpatioTemporalESN._predictProcess.predictQueue = predictQueue
        SpatioTemporalESN._predictProcess.self = self

    def _predictProcess(self, indices):
        inputData = self.sharedNamespace.inputData
        transientTime = self.sharedNamespace.transientTime

        y, x = indices
        workerID = self.parallelWorkerIDs.get()
        #get internal id
 
        id = self._uniqueIDFromIndices(indices)

        #create patchedInputData
        #treat the frame pixels in a special way
        inData = inputData[:, y-self._filterWidth:y+self._filterWidth+1, x-self._filterWidth:x+self._filterWidth+1][:, ::self._stride, ::self._stride].reshape(len(inputData), -1)

        self._x[workerID] = self.sharedNamespace.xs[id]

        #now fit
        X = self.propagate(inData, transientTime, x=self._x[workerID], verbose=0)
        self.sharedNamespace.xs[id] = self._x[workerID]
        
        if self._averageOutputWeights:
            WOut = self._WOut
        else:
            WOut = self._WOuts[id]

        #calculate the actual prediction
        prediction = self.out_activation(B.dot(WOut, X).T)[:, 0]
            
        #store the state and the output matrix of the worker
        SpatioTemporalESN._predictProcess.predictQueue.put(([x-self._filterWidth for x in indices], prediction))

        self.parallelWorkerIDs.put(workerID)