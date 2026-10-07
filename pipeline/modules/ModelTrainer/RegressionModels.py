import pandas as pd
import numpy as np
import re, os, math, copy, pickle
from datetime import datetime

from sklearn.linear_model import LinearRegression
from sklearn.tree import DecisionTreeRegressor 
from sklearn.metrics import mean_squared_error, mean_absolute_error, mean_absolute_percentage_error


from sklearn.model_selection import train_test_split 
from sklearn import metrics 
from sklearn.utils import shuffle

import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import layers
try:  # removed in TensorFlow 2.16+; not needed by the models here
    from tensorflow.keras.layers.experimental import preprocessing
except ImportError:
    preprocessing = None
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3' 

from .RegressionMetrics import get_scores


np.random.seed(2048)
tf.random.set_seed(2048)


PRINT_RESULT = False

def get_adjust_factor(y_val_pca, y_pred_pca):
    
    # Only Under Predictions
    under_g=[]
    under_p=[]
    for g, p in zip(y_val_pca, y_pred_pca):
        if p < g:
            under_g.append(g)
            under_p.append(p)
    adjustF = 1.0
    if len(under_p) > 0:
        adjustF = 1.0 + round(float(mean_absolute_percentage_error(under_g, under_p)), 2)
    print("** AD FACTOR **", adjustF)
    return adjustF


def linear_regression(X_train_pca, X_test_pca, X_val_pca, y_train_pca, y_test_pca, y_val_pca):
    # Train Model
    model = LinearRegression()
    model.fit(X_train_pca,y_train_pca)
    
    # Evaluate Model
    y_pred_pca = model.predict(X_test_pca)
    res_val_pca = []
    for p, a in zip(y_pred_pca, y_test_pca):
        res_val_pca.append([p, a])
    res_pca = pd.DataFrame(res_val_pca, columns=["predic", "actual"])

    if PRINT_RESULT: print("\n--------------------LINEAR REGRESSION-----------------------")
    mse, mae, mape, upp, up_mape, ov_mape = get_scores(y_test_pca, y_pred_pca, printIt=PRINT_RESULT)
    nonadj_scores = (mse, mae, mape, upp, up_mape, ov_mape)
    y_val_pred_pca = model.predict(X_val_pca)
    ADJ_FACT = get_adjust_factor(y_val_pca, y_val_pred_pca)
    y_pred_pca = y_pred_pca*ADJ_FACT
    mse, mae, mape, upp, up_mape, ov_mape = get_scores(y_test_pca, y_pred_pca, printIt=PRINT_RESULT)
    adj_scores = (mse, mae, mape, upp, up_mape, ov_mape)
    if PRINT_RESULT: print("\n-----------------------------------------------------------")

    return model, nonadj_scores, ADJ_FACT, adj_scores

def neural_net(X_train_pca, X_test_pca, X_val_pca, y_train_pca, y_test_pca, y_val_pca):
    # NN Model with one dimention TRAIN
    model = tf.keras.Sequential()
    model.add(tf.keras.layers.Dense(128, input_dim=X_train_pca.shape[1], activation='relu'))
    model.add(tf.keras.layers.Dense(64, activation='relu'))
    model.add(tf.keras.layers.Dense(1, activation='linear'))
    opt = tf.keras.optimizers.Adam(learning_rate=0.01)
    model.compile(loss='mse', optimizer=opt, metrics=['mse', 'mae'])
    history = model.fit(X_train_pca, y_train_pca, epochs=50, validation_split=0.2, batch_size=20, verbose=0)

    # Evaluate Model
    y_pred_pca = model.predict(X_test_pca).flatten()
    res_val_pca = []
    for p, a in zip(y_pred_pca, y_test_pca):
        res_val_pca.append([p, a])
    res_pca = pd.DataFrame(res_val_pca, columns=["predic", "actual"])
    
    if PRINT_RESULT: print("\n--------------------LINEAR REGRESSION-----------------------")
    mse, mae, mape, upp, up_mape, ov_mape = get_scores(y_test_pca, y_pred_pca, printIt=PRINT_RESULT)
    nonadj_scores = (mse, mae, mape, upp, up_mape, ov_mape)
    y_val_pred_pca = model.predict(X_val_pca)
    ADJ_FACT = get_adjust_factor(y_val_pca, y_val_pred_pca)
    y_pred_pca = y_pred_pca*ADJ_FACT
    mse, mae, mape, upp, up_mape, ov_mape = get_scores(y_test_pca, y_pred_pca, printIt=PRINT_RESULT)
    adj_scores = (mse, mae, mape, upp, up_mape, ov_mape)
    if PRINT_RESULT: print("\n-----------------------------------------------------------")

    return model, nonadj_scores, ADJ_FACT, adj_scores


def decision_tree_regressor(X_train_pca, X_test_pca, X_val_pca, y_train_pca, y_test_pca, y_val_pca):
    # create a regressor object
    model = DecisionTreeRegressor(random_state = 0) 
    
    # fit the regressor with X and Y data
    model.fit(X_train_pca, y_train_pca)

    # Evaluate Model
    y_pred_pca = model.predict(X_test_pca).flatten()
    res_val_pca = []
    for p, a in zip(y_pred_pca, y_test_pca):
        res_val_pca.append([p, a])
    res_pca = pd.DataFrame(res_val_pca, columns=["predic", "actual"])

    if PRINT_RESULT: print("\n--------------------LINEAR REGRESSION-----------------------")
    mse, mae, mape, upp, up_mape, ov_mape = get_scores(y_test_pca, y_pred_pca, printIt=PRINT_RESULT)
    nonadj_scores = (mse, mae, mape, upp, up_mape, ov_mape)
    y_val_pred_pca = model.predict(X_val_pca)
    ADJ_FACT = get_adjust_factor(y_val_pca, y_val_pred_pca)
    y_pred_pca = y_pred_pca*ADJ_FACT
    mse, mae, mape, upp, up_mape, ov_mape = get_scores(y_test_pca, y_pred_pca, printIt=PRINT_RESULT)
    adj_scores = (mse, mae, mape, upp, up_mape, ov_mape)
    if PRINT_RESULT: print("\n-----------------------------------------------------------")

    return model, nonadj_scores, ADJ_FACT, adj_scores
    
    
