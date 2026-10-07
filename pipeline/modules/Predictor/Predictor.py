from PipelineModule import PipelineModule

import pandas as pd
import numpy as np
import os, pickle, re,  math, copy
from datetime import datetime


import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import layers
try:  # removed in TensorFlow 2.16+; not needed by the models here
    from tensorflow.keras.layers.experimental import preprocessing
except ImportError:
    preprocessing = None

from sklearn.metrics import mean_squared_error, mean_absolute_error, mean_absolute_percentage_error
from sklearn.model_selection import train_test_split 
from sklearn import metrics 
from sklearn.utils import shuffle

from .RegressionMetrics import get_scores

np.random.seed(2048)
tf.random.set_seed(2048)

REG_MODELS = ["LR", "NN", "DTR"]
DS_COFIG = ["SD", "SD+25FS", "SD+50FS", "SD+75FS"]

PRINT_RESULT=True


class Predictor():
    def __init__(self, config: dict):
        super().__init__()
        self.config = config
        self.application = str.lower(self.config["application"])
        self.application_category = str.lower(self.config["application_category"])
        
        self.dataset = None
        
        # self.savepath_one =  os.path.join(os.getenv("PIPELINE_HOME"), "applications", self.application)
        self.savepath_one =  os.path.join(os.getenv("HARP_STORE"), "applications", "basic", self.application)
        # self.savepath_two =  self.config["cheetah_app_directory"]
        self.savefile = "predictions.csv"
        self.columns = None
        self.estimate = None
        self.test = None
        self.orig = None
        
        self.models_home = None
        self.model = None
        self.dsType = None
        self.modelType = None
        self.VA = None

        
    def load_dataset(self): 
        metadata = self.config["pipeline_config"]
        if not os.path.isfile(metadata["model_commons_filename"]):
            print(f"ERROR: ModelTrainer was unable to find a dataset for `{self.application}`")
            exit()
        self.dataset = pd.read_csv(metadata["model_commons_filename"])
        self.columns = self.dataset.columns.tolist()[1:]
        if not os.path.isfile(metadata["dataset_pca"]):
            print(f"ERROR: ModelTrainer was unable to find a dataset for `{self.application}`")
            exit()
        self.estimate = pd.read_csv(metadata["dataset_pca"])
        if not os.path.isfile(metadata["dataset"]):
            print(f"ERROR: ModelTrainer was unable to find a dataset for `{self.application}`")
            exit()
        self.estimateOrig = pd.read_csv(metadata["dataset"])
        self.test = self.estimate[(self.estimate['run_type'] == "test_data")] 
        self.orig = self.estimateOrig[(self.estimateOrig['run_type'] == "test_data")] 
        
        # self.models_home = os.path.join(os.getenv("PIPELINE_HOME"), "applications", self.application,"models")
        self.models_home = os.path.join(os.getenv("HARP_STORE"), "applications", "basic", self.application,"models")




    def get_train_X_y(self, df_pca):
        scale_label = ['run_type', 'uniq_id']
        target_col='walltime'
        all_cols=df_pca.columns
        needed_cols = [i for i in all_cols if i not in scale_label and i != target_col]

        X_pca = df_pca[needed_cols] # Features
        y_pca = df_pca[target_col] # Target variable
        return (X_pca, y_pca)
        
        
    def policy(self, model="basic"):
        if model == "basic":
            print(self.columns)
            # select the min value of a column in df
            result = None
            min_value = self.dataset.UPP.min()
            level1 = self.dataset[(self.dataset["UPP"]==min_value)]
            result = level1
            if level1.shape[0]>1:
                min_value = self.dataset.OV_MAPE.min()
                level2 = self.dataset[(self.dataset["OV_MAPE"]==min_value)]
                result = level2.iloc[0]
            else:
                result = level1.iloc[0]
            self.model = result.MODEL_NAME
            self.dsType = result.DataSet
            self.modelType = result.RegModel
            self.VA = result.ADJ_FACTOR
            
            
    def apply_model(self):
        # Get oath for models
        models_home = self.models_home+"/"
        model_name = self.model
        load_model = None
        loaded_model = None
        if self.modelType == "LR" or self.modelType == "DTR": #h5 or pkl
            print("I AM HERE")
            load_model = models_home + self.model + ".pkl"
            loaded_model = pickle.load(open(load_model, 'rb'))
        elif self.modelType == "NN":
            load_model = models_home + self.model + ".h5"
            loaded_model = tf.keras.models.load_model(load_model)
        else:
            load_model = models_home + self.model + ".h5"
            loaded_model = tf.keras.models.load_model(load_model)
        
        
        X_pca, y_pca = self.get_train_X_y(self.test)
        
        y_pred_pca = loaded_model.predict(X_pca)
        y_pred_pca = y_pred_pca*self.VA
        y_pred_pca = y_pred_pca.flatten()
        
        mse, mae, mape, upp, up_mape, ov_mape = get_scores(y_pca, y_pred_pca, printIt=PRINT_RESULT)
        
        res_val_pca = []
        
        testres = self.test.copy()
        
        testres["prediction"] = y_pred_pca
        
        orig_cols = list(self.orig.columns)+['prediction']
        
        res = testres[["uniq_id", "prediction"]]
        self.results = self.orig.merge(res, how='inner', on='uniq_id')
        
        self.results.to_csv(self.savepath_one+'/'+self.savefile, header=True, index=False)
        # self.results.to_csv(self.savepath_two+'/'+self.savefile, header=True, index=False)

        # f = open(self.savepath_two+"/prediction_summary.txt", "w")
        # f.write("*** The details of the model and dataset used for predictions ***")
        # f.write("\nThe saved model used for predictions:"+self.model)
        # f.write("\nThe dataset used for predictions:"+self.dsType)
        # f.write("\nThe Regression model used for predictions:"+self.modelType)
        # f.write("\nThe validation adjustment used for predictions:"+str(self.VA))
        # f.close()
    
    
    def execute(self):
        #TODO: write MAE, MSE, etc values to csv/ maintain a table
        # return models, data from train
        
        # Load the dataset load_dataset
        self.load_dataset()
        
        if self.application_category == "basic":
            self.policy()
            self.apply_model()
        
        else:
            print(f"Unable to train model for invalid application `{self.application}`")
            exit()

        print("INFO: Predictor made a choice completed successfully")
    
    
   
