"""
Builds one binary classifier for every user, and fits it to their data
(and random negative examples from other users).
"""

import signal
import os

import argparse
import numpy as np
from tqdm import tqdm

from keras import optimizers
from keras.models import Sequential
from keras.layers import Dense, Flatten, Dropout
from keras.layers import Conv1D
from keras.callbacks import Callback, ModelCheckpoint

from sklearn.metrics import confusion_matrix

import utils

# Hyperparameters
EPOCHS = 25
DROPOUT_RATE = 0.0
BATCH_SIZE = 32
LEARNING_RATE = 3e-4

# Global variables
stop_flag = False  # Flag to indicate that training was terminated early
training_complete = False  # Flag to indicate that training is complete


class TerminateOnFlag(Callback):
    """
    Callback that terminates training at the end of an epoch if stop_flag is encountered.
    """

    def on_batch_end(self, batch, logs=None):
        if stop_flag:
            self.model.stop_training = True


def handler(signum, frame):
    """
    Flags stop_flag if CTRL-C is received.
    """
    global training_complete

    if not training_complete:
        print('\nCTRL+C signal received. Training will finish after current batch.')
        global stop_flag
        stop_flag = True
    else:
        exit()


def build_model(input_shape):
    """
    Builds classifier model (CNN + Dense)
    """

    model = Sequential()

    # Convolutional layers
    model.add(Conv1D(128, 2, activation="relu", input_shape=input_shape))
    model.add(Dropout(rate=DROPOUT_RATE))
    model.add(Conv1D(64, 2, activation="relu"))
    model.add(Dropout(rate=DROPOUT_RATE))

    model.add(Flatten())

    # Dense layers
    model.add(Dense(32, activation="relu"))
    model.add(Dropout(DROPOUT_RATE))
    model.add(Dense(16, activation="relu"))
    model.add(Dropout(DROPOUT_RATE))
    model.add(Dense(1, activation="sigmoid"))

    # print(model.summary())

    return model


def setup_callbacks(save_path, user_nr):
    """
    Sets up callbacks for early stopping and model saving.
    """

    signal.signal(signal.SIGINT, handler)

    callback_list = []

    callback_list.append(TerminateOnFlag())  # Terminate training if CTRL+C

    if save_path is not None:
        model_checkpoint = ModelCheckpoint(save_path + "user_" + str(user_nr) + "_model_{epoch:02d}_{val_loss:.2f}.hdf5",
                                           monitor="val_loss", save_best_only=True, verbose=1, period=10)  # Save model every 5 epochs
        callback_list.append(model_checkpoint)

    return callback_list


def compute_FAR_FRR(trained_model, X_test, y_test):
    """
    Computes the FAR and FRR of trained_model on the given test set.

    Assumes label is [-1, ..., -1] is user is unknown (i.e. unauthorized)
    """

    n_examples = X_test.shape[0]

    n_imposter_tries = 0
    n_valid_tries = 0
    FA_errors = 0
    FR_errors = 0

    y_pred_all = trained_model.predict(X_test)

    # Let every person claim to be every user
    for i in tqdm(range(n_examples)):

        y_true = y_test[i]
        y_pred = np.round(y_pred_all[i])

        # If valid user
        if y_true == 1:
            n_valid_tries += 1
            if y_pred == 0:
                FR_errors += 1

        # If imposter
        else:
            n_imposter_tries += 1
            if y_pred == 1:
                FA_errors += 1

    FAR = float(FA_errors)/n_imposter_tries
    FRR = float(FR_errors)/n_valid_tries

    return FAR, FRR


def main():

    parser = argparse.ArgumentParser()
    parser.add_argument(dest="data_path", metavar="DATA_PATH", help="Path to read examples from.")
    parser.add_argument("-s", "--save_path", metavar="SAVE_PATH", default=None, help="Path to save trained model to. If no path is specified checkpoints are not saved.")
    parser.add_argument("-m", "--metrics-path", metavar="METRICS_PATH", default=None, help="Path to save additional performance metrics to (for debugging purposes).")
    args = parser.parse_args()

    if args.save_path is not None:
        if not os.path.isdir(args.save_path):
            response = input("Save path does not exist. Create it? (Y/n) >> ")
            if response.lower() not in ["y", "yes", "1", ""]:
                exit()
            else:
                os.makedirs(args.save_path)

    if args.metrics_path is not None:
        if not os.path.isdir(args.metrics_path):
            response = input("Metrics path does not exist. Create it? (Y/n) >> ")
            if response.lower() not in ["y", "yes", "1", ""]:
                exit()
            else:
                os.makedirs(args.metrics_path)

    file_name = args.data_path.split("/")[-1]
    n_users = int(file_name.split("_")[0])

    accuracies, FARs, FRRs = [], [], []
    for user_nr in range(n_users):

        # Load training and validation data
        X_train, y_train = utils.load_examples_of_user(args.data_path, user_nr, "train")
        X_valid, y_valid = utils.load_examples_of_user(args.data_path, user_nr, "valid")

        # Shuffle the data
        X_train, y_train = utils.shuffle_data(X_train, y_train, one_hot_labels=False)
        X_valid, y_valid = utils.shuffle_data(X_valid, y_valid, one_hot_labels=False)

        # Build model
        input_shape = X_train.shape[1:]
        model = build_model(input_shape)

        # Compile model
        adam_optimizer = optimizers.Adam(lr=LEARNING_RATE)
        model.compile(loss="binary_crossentropy", optimizer=adam_optimizer, metrics=["accuracy"])

        # Setup callbacks for early stopping and model saving
        callback_list = setup_callbacks(args.save_path, user_nr)

        # Train model
        print("Training on user {}".format(user_nr))
        model.fit(X_train, y_train, validation_data=(X_valid, y_valid), batch_size=BATCH_SIZE,
                  epochs=EPOCHS, callbacks=callback_list)
        global training_complete
        training_complete = True

        # Load test data
        X_test, y_test = utils.load_examples_of_user(args.data_path, user_nr, "test")

        # Test model
        print("Evaluating model...")
        loss, accuracy = model.evaluate(X_test, y_test, verbose=1)
        FAR, FRR = compute_FAR_FRR(model, X_test, y_test)

        accuracies.append(accuracy)
        FARs.append(FAR)
        FRRs.append(FRR)

        print("\n---- Test Results - User " + str(user_nr) + " ----")
        print("Loss = {}, Accuracy = {}".format(loss, accuracy))
        print("FAR = {}, FRR = {}\n".format(FAR, FRR))

        # Additional metrics
        if args.metrics_path is not None:

            # Confusion matrix
            y_pred = np.around(model.predict(X_test))
            conf_matrix = confusion_matrix(y_test, y_pred)
            np.savetxt(args.metrics_path + "user_" + str(user_nr) + "_confusion_matrix.txt", conf_matrix)

    print("---- Total Results ----")
    print("Accuracies = {}".format(accuracies))
    print("FARs = {}".format(FARs))
    print("FRRs = {}".format(FRRs))


if __name__ == "__main__":
    main()
