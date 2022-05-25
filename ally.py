import numpy as np
from sklearn.model_selection import train_test_split
import torch
from strategy import Strategy
import numpy as np
from torch import nn
import sys
import torch
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable
from torch.utils.data import DataLoader, Dataset
from copy import deepcopy
from torch.utils.data.dataset import TensorDataset
import pdb
from sklearn.cluster import MiniBatchKMeans
from sklearn.metrics import pairwise_distances
from scipy import stats

class lambdanet(nn.Module):
    
    def __init__(self, input_dim):
        super(lambdanet, self).__init__()
        self.layers = nn.Sequential(
            nn.Linear(input_dim, 64),
            nn.BatchNorm1d(64),
            nn.ReLU(inplace=True),
            nn.Dropout(p=0.3),
            nn.Linear(64, 32),
            nn.BatchNorm1d(32),
            nn.ReLU(inplace=True),
            nn.Dropout(p=0.25),
            nn.Linear(32, 16),
            nn.BatchNorm1d(16),
            nn.ReLU(inplace=True),
            nn.Dropout(p=0.25),
            nn.Linear(16, 1),
            nn.ReLU(inplace=True),
        )

    def forward(self, x):
        return self.layers(x)

class lambdaset(Dataset):
    def __init__(self, X_train, X_test, y_train, y_test, train=True):

        if train:
            self.x_data, self.y_data = X_train, y_train
        else:
            self.x_data, self.y_data = X_test, y_test
    
    def __getitem__(self, i):
        return self.x_data[i], self.y_data[i], i

    def __len__(self):
        return self.y_data.shape[0]

class ALLYSampling(Strategy):
    def __init__(self, X, Y, idxs_lb, net, handler, args, cluster = 'kmeans', epsilon = 0.2, nPrimal = 1, nPat = 6):
        super(ALLYSampling, self).__init__(X, Y, idxs_lb, net, handler, args)
        
        #self.lambdas = np.ones(sum(self.idxs_lb))
        self.lambdas = np.zeros(sum(self.idxs_lb))
 
        self.seed = args["seed"]
        self.nClasses = args["nClasses"]
        self.nPat = nPat
        self.epsilon = epsilon
        self.lr_dual = args["lr_dual"]
        self.cluster = cluster
        self.nPrimal = nPrimal # Not used in minimal version with alternate primaldual (nPrimal = 1)

    def query(self, n):
        idxs_unlabeled = np.arange(self.n_pool)[~self.idxs_lb]
        idxs_lb = np.arange(self.n_pool)[self.idxs_lb]

        # Prepare data
        X_train, X_test, y_train, y_test = self.prepare_data_lambda(self.X[idxs_lb], self.Y.numpy()[idxs_lb])

        # Train Lambdanet
        self.reg = lambdanet(input_dim = self.net.get_embedding_dim()).cuda()
        self.train_test_lambdanet(X_train, X_test, y_train, y_test)

        # Predict on unlabeled samples
        print("Generating Embdeddings...")
        X_embedding = self.get_embedding(self.X[idxs_unlabeled], self.Y.numpy()[idxs_unlabeled]).numpy()
        preds = self.predict_lambdas(X_embedding)
        
        # Sort samples by lambda
        idxs_lambdas_descending = (-preds).argsort()
        
        # Select samples with highest predicted lambda from each cluster
        if self.cluster == "kmeans":
            # K-means on embeddings
            print("Clustering....")
            nClusters = n
            kmeans = MiniBatchKMeans(n_clusters = nClusters, random_state = self.seed, batch_size=1024)
            cluster_idxs = kmeans.fit_predict(X_embedding)
    
            # Select highest lambdas from each cluster
            chosen = []
            space_in_clust = np.zeros(nClusters)+n//nClusters
            for sample_idx in idxs_lambdas_descending:
                if space_in_clust[cluster_idxs[sample_idx]] > 0:
                    chosen.append(sample_idx)
                    space_in_clust[cluster_idxs[sample_idx]] -= 1
                if len(chosen) >= n:
                    break     
            
        # Select sample with highest predicted lambda from each cluster
        else:
            chosen = idxs_lambdas_descending[:n]

        return idxs_unlabeled[chosen]

    def prepare_data_lambda(self, X, Y):
        X_embedding = self.get_embedding(X, Y).numpy()
        y_lambdas = self.lambdas
        X_train, X_test, y_train, y_test = train_test_split(X_embedding, y_lambdas, test_size=0.12, random_state = self.seed)
        return X_train, X_test, y_train, y_test

    def _train_lambdanet(self, epoch, loader_tr, optimizer):
        self.reg.train()
        mseFinal = 0.

        for batch_idx, (x, y, idxs) in enumerate(loader_tr):
            x, y = Variable(x.cuda().float()), Variable(y.cuda().float())
            optimizer.zero_grad()
            out = self.reg(x)
            loss = F.mse_loss(out.squeeze(), y)
            loss.backward()

            mseFinal += loss.item()            
            optimizer.step()
        return mseFinal/len(loader_tr)

    def train_test_lambdanet(self, X_train, X_test, y_train, y_test):

        optimizer = optim.Adam(self.reg.parameters(), lr = 0.001, weight_decay=0)

        loader_tr = DataLoader(lambdaset(X_train, X_test, y_train, y_test, train = True), batch_size = 64, shuffle = False, drop_last=True)

        #Train
        self.reg.train()
        epoch = 1
        mseCurrent = 10.
        while (mseCurrent > 0.04) and (epoch < 70): #default values for SVHN
            mseCurrent = self._train_lambdanet(epoch, loader_tr, optimizer)
            print(f"{epoch} lambda training mse:  {mseCurrent:.3f}", flush=True)
            epoch += 1
               
        mseFinal = 0.

        #Test
        P = self.predict_lambdas(X_test, y_test)
        mseTest = F.mse_loss(P, torch.tensor(y_test))           
        print(f"-----> lambda test mse: {mseTest.item():.2f}\n", flush=True)
        return None
	

    def predict_lambdas(self, X, Y=None):
        
        if Y is None:
            Y = np.zeros(len(X))
        loader_te = DataLoader(lambdaset(None, X, None, Y, train = False), batch_size = 64, shuffle = False, drop_last=True)

        self.reg.eval()       
        P = torch.zeros(len(Y))
        with torch.no_grad():
            for x, y, idxs in loader_te:
                x, y = Variable(x.cuda().float()), Variable(y.cuda().float())
                out = self.reg(x)
                P[idxs] = out.squeeze().data.cpu()
        return P