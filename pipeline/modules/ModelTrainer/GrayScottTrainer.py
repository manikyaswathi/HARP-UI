import pandas as pd
import numpy as np
import re, os, math, copy

from sklearn.linear_model import LinearRegression
from sklearn.tree import DecisionTreeRegressor 
from sklearn.metrics import mean_squared_error, mean_absolute_error

from sklearn.model_selection import train_test_split # Import train_test_split function
from sklearn import metrics #Import scikit-learn metrics module for accuracy calculation


import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import layers
try:  # removed in TensorFlow 2.16+; not needed by the models here
    from tensorflow.keras.layers.experimental import preprocessing
except ImportError:
    preprocessing = None


def MAPE(gold, predict):
    overall=0
    for g, p in zip(gold, predict):
        overall+=abs(g-p)/g
    overall=overall/len(gold)
    return overall


def MAPE_i(gold, predict):
    overall=[]
    for g, p in zip(gold, predict):
        overall.append(abs(g-p)/g)
    return overall

def linear_regression(X_train_pca, X_test_pca, y_train_pca, y_test_pca):
    linear_regress_pca = LinearRegression()
    linear_regress_pca.fit(X_train_pca,y_train_pca)
    y_pred_pca = linear_regress_pca.predict(X_test_pca)

    print("----------------------Linear Regression-----------------------------")
    print("Mean Squared Error:", mean_squared_error(y_test_pca, y_pred_pca))
    print("Mean Absolute Error:", mean_absolute_error(y_test_pca, y_pred_pca))
    print("MAPE:", MAPE(y_test_pca, y_pred_pca))
    print("--------------------------------------------------------------------")

    return linear_regress_pca

def evaluate_neural_net(model, X_train_pca, X_test_pca, y_train_pca, y_test_pca):
    model.evaluate(X_train_pca, y_train_pca, batch_size=20)
    model.evaluate(X_test_pca, y_test_pca, batch_size=20)

    y_pred_pca_2 = model.predict(X_test_pca).flatten()
    testy_np = y_test_pca.to_numpy()

    print("----------------------Neural Net------------------------------------")
    print("Mean Squared Error:", mean_squared_error(y_test_pca, y_pred_pca_2))
    print("Mean Absolute Error:", mean_absolute_error(y_test_pca, y_pred_pca_2))
    print("MAPE:", MAPE(y_test_pca, y_pred_pca_2))
    print("--------------------------------------------------------------------")


def neural_net(X_train_pca, X_test_pca, y_train_pca, y_test_pca):
    # NN Model with one dimention
    model = tf.keras.Sequential()
    model.add(tf.keras.layers.Dense(128, input_dim=X_train_pca.shape[1], activation='relu'))
    model.add(tf.keras.layers.Dense(64, activation='relu'))
    # model.add(tf.keras.layers.Dense(32, activation='relu'))
    # model.add(tf.keras.layers.Dense(16, activation='relu'))
    # model.add(tf.keras.layers.Dense(4, activation='relu'))
    model.add(tf.keras.layers.Dense(1, activation='linear'))
    opt = tf.keras.optimizers.Adam(learning_rate=0.01)
    model.compile(loss='mse', optimizer=opt, metrics=['mse', 'mae'])
    history = model.fit(X_train_pca, y_train_pca, epochs=300, validation_split=0.2, batch_size=20)

    evaluate_neural_net(model, X_train_pca, X_test_pca, y_train_pca, y_test_pca)
    return model

def decision_tree_regressor(X_train_pca, X_test_pca, y_train_pca, y_test_pca):
    # create a regressor object
    regressor = DecisionTreeRegressor(random_state = 0) 
    
    # fit the regressor with X and Y data
    regressor.fit(X_train_pca, y_train_pca)

    y_pred_pca_3 = regressor.predict(X_test_pca)

    print("--------------------Decision Tree Regressor--------------------------")
    print("Mean Squared Error:", mean_squared_error(y_test_pca, y_pred_pca_3))
    print("Mean Absolute Error:", mean_absolute_error(y_test_pca, y_pred_pca_3))
    print("MAPE:", MAPE(y_test_pca, y_pred_pca_3))
    print("--------------------------------------------------------------------")

    return regressor

def train(dataset_path: str):
    df_pca = pd.read_csv(dataset_path)
    # print(df_pca.head())
    feature_cols_pcs = [str(i) for i in range(0,13)]
    label_pca='walltime'
    X_pca = df_pca[feature_cols_pcs] # Features
    y_pca = df_pca[label_pca] # Target variable

    X_train_pca, X_test_pca, y_train_pca, y_test_pca = train_test_split(X_pca, y_pca, test_size=0.3, random_state=1) 

    lr_model = linear_regression(X_train_pca, X_test_pca, y_train_pca, y_test_pca)
    dnn_model = neural_net(X_train_pca, X_test_pca, y_train_pca, y_test_pca)
    dt_model = decision_tree_regressor(X_train_pca, X_test_pca, y_train_pca, y_test_pca)

    #TODO SAVE MODELS
    #TODO CHOOSE BEST MODEL
