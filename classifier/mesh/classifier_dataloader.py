import sys
sys.path.insert(0, '/SPINH')
from sklearn.utils import shuffle
import torch
import pandas as pd
from sklearn.preprocessing import StandardScaler
from torch.utils.data import Dataset
from classifier.classifier_config import args


# Define the DataLoader Class

class Classifier_Dataset(Dataset):
    def __init__(self, data_path):
        self.data_path = data_path
        self._load_dataset()
    
    def _load_dataset(self):
        # Read Data
        data = pd.read_csv(self.data_path)
        self.sp_op = data.iloc[:,:14].values
        self.label_m = data.iloc[:,14].values
        # Standardize
        self.mean = torch.tensor(args.mean, dtype=torch.float)
        self.std = torch.tensor(args.std, dtype=torch.float)
        self.sp_op = torch.tensor(self.sp_op, dtype=torch.float)
        self.sp_op = (self.sp_op - self.mean)/torch.sqrt(self.std)

        self.label_m = torch.tensor(self.label_m, dtype=torch.float).unsqueeze(1)


    def __getitem__(self, indesp_op):
        return self.sp_op[indesp_op], self.label_m[indesp_op]

    def __len__(self):
        return len(self.sp_op)