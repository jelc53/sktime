# -*- coding: utf-8 -*-
"""WEASEL classifier.

Dictionary based classifier based on SFA transform, BOSS and linear regression.
"""

__author__ = ["patrickzib", "Arik Ermshaus"]
__all__ = ["WEASEL"]

import math

import numpy as np
from joblib import Parallel, delayed
from numba import njit
from sklearn.feature_extraction import DictVectorizer
from sklearn.feature_selection import chi2
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.utils import check_random_state

from sktime.classification.base import BaseClassifier
from sktime.transformations.panel.dictionary_based import SFA


class WEASEL(BaseClassifier):
    """Word Extraction for Time Series Classification (WEASEL).

    Overview: Input n series length m
    WEASEL is a dictionary classifier that builds a bag-of-patterns using SFA
    for different window lengths and learns a logistic regression classifier
    on this bag.

    There are these primary parameters:
            alphabet_size: alphabet size
            chi2-threshold: used for feature selection to select best words
            anova: select best l/2 fourier coefficients other than first ones
            bigrams: using bigrams of SFA words
            binning_strategy: the binning strategy used to discretise into
                             SFA words.
    WEASEL slides a window length w along the series. The w length window
    is shortened to an l length word through taking a Fourier transform and
    keeping the best l/2 complex coefficients using an anova one-sided
    test. These l coefficients are then discretised into alpha possible
    symbols, to form a word of length l. A histogram of words for each
    series is formed and stored.
    For each window-length a bag is created and all words are joint into
    one bag-of-patterns. Words from different window-lengths are
    discriminated by different prefixes.
    fit involves training a logistic regression classifier on the single
    bag-of-patterns.

    predict uses the logistic regression classifier

    Parameters
    ----------
    anova: boolean, default=True
        If True, the Fourier coefficient selection is done via a one-way
        ANOVA test. If False, the first Fourier coefficients are selected.
        Only applicable if labels are given
    bigrams: boolean, default=True
        whether to create bigrams of SFA words
    binning_strategy: {"equi-depth", "equi-width", "information-gain"},
    default="information-gain"
        The binning method used to derive the breakpoints.
    window_inc: int, default=2
        WEASEL create a BoP model for each window sizes. This is the
        increment used to determine the next window size.
    p_threshold:  int, default=0.05 (disabled by default)
        Feature selection is applied based on the chi-squared test.
        This is the p-value threshold to use for chi-squared test on bag-of-words
        (lower means more strict). 1 indicates that the test
        should not be performed.
    random_state: int or None, default=None
        Seed for random, integer

    Attributes
    ----------
    n_classes_ : int
        The number of classes.
    classes_ : list
        The classes labels.

    See Also
    --------
    MUSE

    References
    ----------
    .. [1] Patrick Schäfer and Ulf Leser, "Fast and Accurate Time Series Classification
    with WEASEL", in proc ACM on Conference on Information and Knowledge Management,
    2017, https://dl.acm.org/doi/10.1145/3132847.3132980

    Notes
    -----
    For the Java version, see
    `TSML <https://github.com/uea-machine-learning/tsml/blob/master/src/main/java
    /tsml/classifiers/dictionary_based/WEASEL.java>`_.

    Examples
    --------
    >>> from sktime.classification.dictionary_based import WEASEL
    >>> from sktime.datasets import load_unit_test
    >>> X_train, y_train = load_unit_test(split="train", return_X_y=True)
    >>> X_test, y_test = load_unit_test(split="test", return_X_y=True)
    >>> clf = WEASEL(window_inc=4)
    >>> clf.fit(X_train, y_train)
    WEASEL(...)
    >>> y_pred = clf.predict(X_test)
    """

    _tags = {
        "capability:multithreading": True,
        "classifier_type": "dictionary",
    }

    def __init__(
        self,
        anova=True,
        bigrams=True,
        binning_strategy="information-gain",
        window_inc=2,
        p_threshold=0.05,
        n_jobs=1,
        random_state=None,
    ):

        # currently greater values than 4 are not supported.
        self.alphabet_size = 4

        # feature selection is applied based on the chi-squared test.
        self.p_threshold = p_threshold

        self.anova = anova

        self.norm_options = [False]
        self.word_lengths = [4, 6]

        self.bigrams = bigrams
        self.binning_strategy = binning_strategy
        self.random_state = random_state

        self.min_window = 6
        self.max_window = 100

        self.window_inc = window_inc
        self.highest_bit = -1
        self.window_sizes = []

        self.series_length = 0
        self.n_instances = 0

        self.SFA_transformers = []
        self.clf = None
        self.n_jobs = n_jobs

        super(WEASEL, self).__init__()

    def _fit(self, X, y):
        """Build a WEASEL classifiers from the training set (X, y).

        Parameters
        ----------
        X : 3D np.array of shape = [n_instances, n_dimensions, series_length]
            The training data.
        y : array-like, shape = [n_instances]
            The class labels.

        Returns
        -------
        self :
            Reference to self.
        """
        # Window length parameter space dependent on series length
        self.n_instances, self.series_length = X.shape[0], X.shape[-1]

        win_inc = self._compute_window_inc()

        self.max_window = int(min(self.series_length, self.max_window))
        if self.min_window > self.max_window:
            raise ValueError(
                f"Error in WEASEL, min_window ="
                f"{self.min_window} is bigger"
                f" than max_window ={self.max_window},"
                f" series length is {self.series_length}"
                f" try set min_window to be smaller than series length in "
                f"the constructor, but the classifier may not work at "
                f"all with very short series"
            )
        self.window_sizes = list(range(self.min_window, self.max_window, win_inc))
        self.highest_bit = (math.ceil(math.log2(self.max_window))) + 1

        def _parallel_fit(
            window_size,
        ):
            rng = check_random_state(window_size)
            all_words = [dict() for x in range(len(X))]
            relevant_features_count = 0

            # for window_size in self.window_sizes:
            transformer = SFA(
                word_length=rng.choice(self.word_lengths),
                alphabet_size=self.alphabet_size,
                window_size=window_size,
                norm=rng.choice(self.norm_options),
                anova=self.anova,
                # levels=rng.choice([1, 2, 3]),
                binning_method=self.binning_strategy,
                bigrams=self.bigrams,
                remove_repeat_words=False,
                lower_bounding=False,
                save_words=False,
            )

            sfa_words = transformer.fit_transform(X, y)

            # self.SFA_transformers.append(transformer)
            bag = sfa_words[0]
            apply_chi_squared = self.p_threshold < 1

            # chi-squared test to keep only relevant features
            if apply_chi_squared:
                vectorizer = DictVectorizer(sparse=True, dtype=np.int32, sort=False)
                bag_vec = vectorizer.fit_transform(bag)

                chi2_statistics, p = chi2(bag_vec, y)
                relevant_features_idx = np.where(p <= self.p_threshold)[0]
                relevant_features = set(
                    np.array(vectorizer.feature_names_)[relevant_features_idx]
                )
                relevant_features_count += len(relevant_features_idx)

                # merging bag-of-patterns of different window_sizes
                # to single bag-of-patterns with prefix indicating
                # the used window-length
                for j in range(len(bag)):
                    for (key, value) in bag[j].items():
                        # chi-squared test
                        if (not apply_chi_squared) or (key in relevant_features):
                            # append the prefixes to the words to
                            # distinguish between window-sizes
                            word = WEASEL._shift_left(
                                key, self.highest_bit, window_size
                            )
                            all_words[j][word] = value

                return all_words, transformer, relevant_features_count

        parallel_res = Parallel(n_jobs=self._threads_to_use)(
            delayed(_parallel_fit)(window_size) for window_size in self.window_sizes
        )  # , verbose=self.verbose

        relevant_features_count = 0
        all_words = [dict() for x in range(len(X))]

        for sfa_words, transformer, rel_features_count in parallel_res:
            transformer.n_jobs = self._threads_to_use
            self.SFA_transformers.append(transformer)
            relevant_features_count += rel_features_count

            for idx, bag in enumerate(sfa_words):
                for word, count in bag.items():
                    all_words[idx][word] = count

        self.clf = make_pipeline(
            DictVectorizer(sparse=True, sort=False),
            # StandardScaler(copy=False),
            LogisticRegression(
                max_iter=5000,
                solver="liblinear",
                dual=True,
                # class_weight="balanced",
                penalty="l2",
                random_state=self.random_state,
                n_jobs=self._threads_to_use,
            ),
        )

        # print("Size of dict", relevant_features_count)
        self.clf.fit(all_words, y)

        return self

    def _predict(self, X) -> np.ndarray:
        """Predict class values of n instances in X.

        Parameters
        ----------
        X : 3D np.array of shape = [n_instances, n_dimensions, series_length]
            The data to make predictions for.

        Returns
        -------
        y : array-like, shape = [n_instances]
            Predicted class labels.
        """
        bag = self._transform_words(X)
        return self.clf.predict(bag)

    def _predict_proba(self, X) -> np.ndarray:
        """Predict class probabilities for n instances in X.

        Parameters
        ----------
        X : 3D np.array of shape = [n_instances, n_dimensions, series_length]
            The data to make predict probabilities for.

        Returns
        -------
        y : array-like, shape = [n_instances, n_classes_]
            Predicted probabilities using the ordering in classes_.
        """
        bag = self._transform_words(X)
        return self.clf.predict_proba(bag)

    def _transform_words(self, X):
        bag_all_words = [dict() for _ in range(len(X))]
        for transformer in self.SFA_transformers:
            # SFA transform
            sfa_words = transformer.transform(X)
            bag = sfa_words[0]

            # merging bag-of-patterns of different window_sizes
            # to single bag-of-patterns with prefix indicating
            # the used window-length
            for j in range(len(bag)):
                for (key, value) in bag[j].items():
                    # append the prefices to the words to distinguish
                    # between window-sizes
                    word = WEASEL._shift_left(
                        key, self.highest_bit, transformer.window_size
                    )
                    bag_all_words[j][word] = value

        return bag_all_words

    def _compute_window_inc(self):
        win_inc = self.window_inc
        if self.series_length < 100:
            win_inc = 1  # less than 100 is ok runtime-wise
        return win_inc

    @staticmethod
    @njit("int64(int64,int64,int64)", fastmath=True, cache=True)
    def _shift_left(key, highest_bit, window_size):
        return (key << highest_bit) | window_size

    @classmethod
    def get_test_params(cls, parameter_set="default"):
        """Return testing parameter settings for the estimator.

        Parameters
        ----------
        parameter_set : str, default="default"
            Name of the set of test parameters to return, for use in tests. If no
            special parameters are defined for a value, will return `"default"` set.
            For classifiers, a "default" set of parameters should be provided for
            general testing, and a "results_comparison" set for comparing against
            previously recorded results if the general set does not produce suitable
            probabilities to compare against.

        Returns
        -------
        params : dict or list of dict, default={}
            Parameters to create testing instances of the class.
            Each dict are parameters to construct an "interesting" test instance, i.e.,
            `MyClass(**params)` or `MyClass(**params[i])` creates a valid test instance.
            `create_test_instance` uses the first (or only) dictionary in `params`.
        """
        return {"window_inc": 4}
