import os
import argparse

import numpy as np
from keras.models import load_model, Model
from keras.layers import Input, Lambda, Dense, Dropout
from keras.models import Sequential
from keras.utils import CustomObjectScope
from keras import optimizers
import keras.backend as K


import utils
import cnn_siamese_online

# Hyperparameters
LEARNING_RATE = 1e-2
BATCH_SIZE = 10000
EPOCHS = 100
DROPOUT_RATE = 0.2


def build_pair_distance_model(tower_model, input_shape):
    """
    Builds a model that takes a triplet as input and returns
    abs(A - P) and abs(A - N)
    """

    input_anchor = Input(input_shape)
    input_positive = Input(input_shape)
    input_negative = Input(input_shape)

    embedd_anchor = tower_model(input_anchor)
    embedd_positive = tower_model(input_positive)
    embedd_negative = tower_model(input_negative)

    tower_output_shape = tower_model.layers[-1].output_shape

    abs_difference = Lambda(lambda z: K.abs(z[0] - z[1]), output_shape=tower_output_shape)

    positive_pair_dist = abs_difference([embedd_anchor, embedd_positive])
    negative_pair_dist = abs_difference([embedd_anchor, embedd_negative])

    pair_distance_model = Model(inputs=[input_anchor, input_positive, input_negative], outputs=[positive_pair_dist, negative_pair_dist])

    return pair_distance_model


def shuffle(X, y):

    perm = np.random.permutation(X.shape[0])
    X = X[perm, :]
    y = y[perm]

    return X, y


def accuracy_FAR_FRR(y_true, y_pred):

    n_examples = y_true.shape[0]

    correct = 0
    FAR_errors = 0
    FRR_errors = 0
    for i in range(n_examples):

        if y_true[i] == np.round(y_pred[i]):
            correct += 1

        elif y_true[i] == 0 and np.round(y_pred[i]) == 1:
            FAR_errors += 1

        elif y_true[i] == 1 and np.round(y_pred[i]) == 0:
            FRR_errors += 1

    accuracy = float(correct) / n_examples
    FAR = float(FAR_errors) / (n_examples - np.sum(y_true))
    FRR = float(FRR_errors) / np.sum(y_true)

    return accuracy, FAR, FRR


def ensemble_accuracy_FAR_FRR(y_true, y_pred, ensemble_size):

    n_examples = y_true.shape[0]
    n_actual_examples = len(range(0, n_examples, 9))

    correct = 0
    FAR_errors = 0
    FRR_errors = 0
    for i in range(0, n_examples - ensemble_size, ensemble_size - 1):

        sum = 0
        for ii in range(ensemble_size - 1):
            sum += np.round(y_pred[i + ii])

        y_pred[i] = int(sum > (ensemble_size // 2))

        if y_true[i] == y_pred[i]:
            correct += 1

        elif y_true[i] == 0 and y_pred[i] == 1:
            FAR_errors += 1

        elif y_true[i] == 1 and y_pred[i] == 0:
            FRR_errors += 1

    accuracy = float(correct) / n_actual_examples
    FAR = float(FAR_errors) / (n_actual_examples - int(np.sum(y_true) / (ensemble_size - 1)))
    FRR = float(FRR_errors) / int(np.sum(y_true) / (ensemble_size - 1))

    return accuracy, FAR, FRR


def build_nn_model(input_shape):
    """
    Builds a neural network classifier model.
    """

    model = Sequential()

    model.add(Dense(32, activation="relu"))
    model.add(Dropout(rate=DROPOUT_RATE))
    model.add(Dense(1, activation="sigmoid"))

    return model


def parse_args(args):
    """
    Checks that input args are valid.
    """

    assert os.path.isfile(args.triplets_path), "The specified triplet file does not exist."
    assert os.path.isfile(args.model_path), "The specified model file does not exist."

    args.ensemble = int(args.ensemble)
    assert args.ensemble <= 100, "Invalid ensemble value. Cannot have an ensemble > 100."


def main():

    # Parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument(dest="triplets_path", metavar="TRIPLETS_PATH", help="Path to read triplets from.")
    parser.add_argument(dest="model_path", metavar="MODEL_PATH", help="Path to read model from.")
    parser.add_argument("-e", "--ensemble", metavar="ENSEMBLE", default=1, help="How many examples to ensemble when predicting. Default: 1")
    parser.add_argument("-b", "--read_batches", metavar="READ_BATCHES", default=False, help="If true, data is read incrementally in batches during training.")
    args = parser.parse_args()
    parse_args(args)

    # Load model
    with CustomObjectScope({'_euclidean_distance': cnn_siamese_online._euclidean_distance,
                            'ALPHA': cnn_siamese_online.ALPHA, "relu_clipped": cnn_siamese_online.relu_clipped}):
        tower_model = load_model(args.model_path)
        tower_model.compile(optimizer='adam', loss='mean_squared_error')  # Model was previously not compiled

    X_shape, y_shape = utils.get_shapes(args.triplets_path, "train_anchors")

    # Build model to compute [A, P, N] => [abs(emb(A) - emb(P)), abs(emb(A) - emb(N))]
    pair_distance_model = build_pair_distance_model(tower_model, X_shape[1:])
    pair_distance_model.compile(optimizer="adam", loss="mean_squared_error")  # Need to compile in order to predict

    if not args.read_batches:  # Read all data at once

        # Load training triplets and validation triplets
        X_train_anchors, _ = utils.load_examples(args.triplets_path, "train_anchors")
        X_train_positives, _ = utils.load_examples(args.triplets_path, "train_positives")
        X_train_negatives, _ = utils.load_examples(args.triplets_path, "train_negatives")
        X_valid_anchors, _ = utils.load_examples(args.triplets_path, "valid_anchors")
        X_valid_positives, _ = utils.load_examples(args.triplets_path, "valid_positives")
        X_valid_negatives, _ = utils.load_examples(args.triplets_path, "valid_negatives")

        # Get abs(distance) of embeddings
        X_train_1, X_train_0 = pair_distance_model.predict([X_train_anchors, X_train_positives, X_train_negatives])
        X_valid_1, X_valid_0 = pair_distance_model.predict([X_valid_anchors, X_valid_positives, X_valid_negatives])

    else:  # Read data in batches

        training_batch_generator = utils.DataGenerator(args.triplets_path, "train", batch_size=100, stop_after_batch=10)
        validation_batch_generator = utils.DataGenerator(args.triplets_path, "valid", batch_size=1000)

        # Get abs(distance) of embeddings (one batch at a time)
        X_train_1, X_train_0 = pair_distance_model.predict_generator(generator=training_batch_generator, verbose=1)
        X_valid_1, X_valid_0 = pair_distance_model.predict_generator(generator=validation_batch_generator, verbose=1)

    # Stack positive and negative examples
    X_train = np.vstack((X_train_1, X_train_0))
    y_train = np.hstack((np.ones(X_train_1.shape[0], ), np.zeros(X_train_0.shape[0],)))
    X_valid = np.vstack((X_valid_1, X_valid_0))
    y_valid = np.hstack((np.ones(X_valid_1.shape[0], ), np.zeros(X_valid_0.shape[0],)))

    # Shuffle the data
    X_train, y_train = shuffle(X_train, y_train)

    # Build neural net classifier
    nn_model = build_nn_model(input_shape=X_train.shape[1:])
    adam_optimizer = optimizers.Adam(lr=LEARNING_RATE)
    nn_model.compile(loss="binary_crossentropy", optimizer=adam_optimizer, metrics=["accuracy"])

    # Train model
    nn_model.fit(X_train, y_train, validation_data=(X_valid, y_valid), batch_size=BATCH_SIZE, epochs=EPOCHS)

    # Evaluate model
    y_pred = nn_model.predict(X_valid)

    if args.ensemble > 1:
        accuracy, FAR, FRR = ensemble_accuracy_FAR_FRR(y_valid, y_pred, args.ensemble)
        print("\n\n---- Validation Results. With ensembling = {}. ----".format(args.ensemble))
    else:
        accuracy, FAR, FRR = accuracy_FAR_FRR(y_valid, y_pred)
        print("\n\n---- Validation Results. No ensembling. ----")

    print("Accuracy = {}".format(accuracy))
    print("FAR = {}".format(FAR))
    print("FRR = {}".format(FRR))


if __name__ == "__main__":
    main()