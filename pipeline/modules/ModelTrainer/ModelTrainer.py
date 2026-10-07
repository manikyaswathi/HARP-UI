from PipelineModule import PipelineModule

import pandas as pd
import os, pickle, re
from datetime import datetime

from sklearn.metrics import mean_squared_error, mean_absolute_error, mean_absolute_percentage_error
from sklearn.model_selection import train_test_split # Import train_test_split function
from sklearn import metrics #Import scikit-learn metrics module for accuracy calculation
from sklearn.utils import shuffle


from .RegressionModels import decision_tree_regressor, neural_net, linear_regression, get_adjust_factor

REG_MODELS = ["LR", "NN", "DTR"]
DS_COFIG = ["SD", "SD+25FS", "SD+50FS", "SD+75FS"]
# Optional subsets, e.g. HARP_MODELS="LR,DTR" HARP_TRAINING_SETS="SD,SD+75FS" (default: all of them)
REG_MODELS = [m for m in os.environ.get("HARP_MODELS", ",".join(REG_MODELS)).split(",") if m in REG_MODELS] or REG_MODELS
DS_COFIG = [d for d in os.environ.get("HARP_TRAINING_SETS", ",".join(DS_COFIG)).split(",") if d in DS_COFIG] or DS_COFIG


class ModelTrainer(PipelineModule):
    def __init__(self, config: dict):
        super().__init__()
        self.application = str.lower(config["application"])
        self.application_category = str.lower(config["application_category"])
        self.config = config
        self.dataset = None
        self.test_data = None
        self.train = None
        self.test = None
        self.validate = None


    def load_dataset(self): 
        metadata = self.config["pipeline_config"]
        if not os.path.isfile(metadata["dataset_pca"]):
            print(f"ERROR: ModelTrainer was unable to find a dataset for `{self.application}`")
            exit()
        self.dataset = pd.read_csv(metadata["dataset_pca"])

    
    def save_models(self, models):
        print("INFO: Saving models...")
        metadata = self.config["pipeline_config"]
        # model_paths = metadata["models"]
        # models_home = os.path.join(os.getenv("PIPELINE_HOME"), "applications", self.application,"models")
        models_home = os.path.join(os.getenv("HARP_STORE"), "applications", "basic", self.application,"models")
        for k,v in models:
            model_path = os.path.join(models_home, k)
            lr_regex = re.compile('^LR_*')
            dtr_regex = re.compile('^DTR_*')
            dnn_regex = re.compile('^NN_*')
            if lr_regex.search(k) or dtr_regex.search(k): #save scikit-learn model
                file = model_path+'.pkl'
                pickle.dump(v, open(file, "wb"))
            elif dnn_regex.search(k): # tensorflow model
                file = model_path+'.h5'
                v.save(file)
                
                
    def save_model_commons_meta(self, df, current_save):
        print("INFO: Saving the stage 2 data as CSV...")
        metadata = self.config["pipeline_config"]
        # model_paths = metadata["models"]
        # models_home = os.path.join(os.getenv("PIPELINE_HOME"), "applications", self.application,"models")
        # models_meta = os.path.join(os.getenv("PIPELINE_HOME"), "applications", self.application)
        models_home = os.path.join(os.getenv("HARP_STORE"), "applications", "basic", self.application,"models")
        models_meta = os.path.join(os.getenv("HARP_STORE"), "applications", "basic", self.application)
        metafile = metadata["model_commons_filename"]
        
        print("models_home", models_home)
        print("models_meta", models_meta)
        print("metafile", metafile)
        
        df.to_csv(models_home+"/"+self.application+"_MC_"+current_save+".csv")
        df.to_csv(metafile)
    
    
    def get_DS_config(self, ds_train_config="SD"): 
        """
        Loop thorigh cofigs and get it
        """
        train_data_sd = self.dataset[(self.dataset['run_type'] == "SD")] 
        train_data_fs = self.dataset[(self.dataset['run_type'] == "FS")] 
        train_data_td = self.dataset[(self.dataset['run_type'] == "test_data")] 
        FS_data, Val_data = train_test_split(train_data_fs, test_size=0.25, random_state=2048) 

        frames = [train_data_sd]
        self.test=train_data_td
        self.validate=Val_data
        if ds_train_config == "SD+25FS":
            FS_dataS, _ = train_test_split(train_data_fs, test_size=0.75, random_state=2048) 
            train_fs = FS_dataS.sample(n=train_data_sd.shape[0], random_state=2048, replace=True)
            train_data_fs = train_fs.copy()
            frames.append(train_data_fs)
            full_training_data = pd.concat(frames)
            self.train=full_training_data
        elif ds_train_config == "SD+50FS":
            FS_dataS, _ = train_test_split(train_data_fs, test_size=0.50, random_state=2048) 
            train_fs = FS_dataS.sample(n=train_data_sd.shape[0], random_state=2048, replace=True)
            train_data_fs = train_fs.copy()
            frames.append(train_data_fs)
            full_training_data = pd.concat(frames)
            self.train=full_training_data
        elif ds_train_config == "SD+75FS":
            train_fs = FS_data.sample(n=train_data_sd.shape[0], random_state=2048, replace=True)
            train_data_fs = train_fs.copy()
            frames.append(train_data_fs)
            full_training_data = pd.concat(frames)
            self.train=full_training_data
        else:
            self.train=train_data_sd
            
            
    def get_train_X_y(self, df_pca):
        scale_label = ['run_type', 'uniq_id']
        target_col='walltime'
        all_cols=df_pca.columns
        needed_cols = [i for i in all_cols if i not in scale_label and i != target_col]

        X_pca = df_pca[needed_cols] # Features
        y_pca = df_pca[target_col] # Target variable
        return (X_pca, y_pca)
    
    
    
    # ["LR", "NN", "DTR"]
    def generate_models(self):
        date_time = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
        model = None
        scores = None
        results=[]
        
        trained_models=[]
        trained_models_names=[]
        
        for mod in REG_MODELS:
            print("TRAINING MODEL:", mod)
            print("data", "train", "test", "validate")
            for ds in DS_COFIG:
                self.get_DS_config(ds_train_config=ds)
                print("********", ds, self.train.shape, self.test.shape, self.validate.shape)
                X_train_pca, y_train_pca = self.get_train_X_y(self.train)
                X_test_pca, y_test_pca = self.get_train_X_y(self.test)
                X_val_pca, y_val_pca = self.get_train_X_y(self.validate)
                print("training models with features:", X_train_pca.shape)
                if mod == "LR":
                    model, noadj_scores, ADJ_FACTOR, adj_scores = linear_regression(X_train_pca, X_test_pca, X_val_pca, y_train_pca, y_test_pca, y_val_pca)
                if mod == "NN":
                    model, noadj_scores, ADJ_FACTOR, adj_scores = neural_net(X_train_pca, X_test_pca, X_val_pca, y_train_pca, y_test_pca, y_val_pca)
                if mod == "DTR":
                    model, noadj_scores, ADJ_FACTOR, adj_scores = decision_tree_regressor(X_train_pca, X_test_pca, X_val_pca, y_train_pca, y_test_pca, y_val_pca)
                mse, mae, mape, upp, up_mape, ov_mape = noadj_scores
                va="no"
                model_name=mod+"_"+ds+"_"+va+"_"+date_time
                trained_models_names.append(model_name)
                results.append([ds, mod, 1.0, mse, mae, mape, upp, up_mape, ov_mape, model_name]) 
                trained_models.append((model_name, model))
                mse, mae, mape, upp, up_mape, ov_mape = adj_scores
                va="yes"
                model_name=mod+"_"+ds+"_"+va+"_"+date_time
                results.append([ds, mod, ADJ_FACTOR, mse, mae, mape, upp, up_mape, ov_mape, model_name]) 
                trained_models.append((model_name, model))
                
        col=["DataSet", "RegModel", "ADJ_FACTOR", "MSE", "MAE", "MAPE", "UPP", "UP_MAPE", "OV_MAPE", "MODEL_NAME"]
        df_pca = pd.DataFrame(results, columns = col) #dtype = float
        current_save = date_time
        self.save_model_commons_meta(df_pca, current_save)
        return trained_models    
    

    def execute(self):
        #TODO: write MAE, MSE, etc values to csv/ maintain a table
        # return models, data from train
        
        # Load the dataset load_dataset
        self.load_dataset()
        
        if self.application_category == "basic":
            self.get_DS_config()
            models = self.generate_models()
            self.save_models(models)
                
        elif self.application_category == "trimmomatic":
            if self.should_train():
                models, pd_df = trimmomatic_train(self.dataset)
                self.save_models(models)
                self.save_model_dataset(pd_df)
            else:
                print(f"Model retraining not required for `{self.application}`")
                return
        elif self.application_category == "dnn":
            if self.should_train():
                models, pd_df = dnn_train(self.dataset)
                self.save_models(models)
                self.save_model_dataset(pd_df)
            else:
                print(f"Model retraining not required for `{self.application}`")
                return
        else:
            print(f"Unable to train model for invalid application `{self.application}`")
            exit()

        print("INFO: ModelTrainer completed successfully")

        
