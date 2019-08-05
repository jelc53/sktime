import numpy as np

__all__ = ['Orchestrator']
__author__ = ['Viktor Kazakov']


class Orchestrator:
    """
    Orchestrates the sequencing of running the machine learning experiments.

    Parameters
    ----------
    tasks: sktime.highlevel.Task
        task object
    datasets: pandas dataframe
        datasets in pandas skitme format
    strategies: list of sktime strategy
        strategy as per sktime.highlevel
    cv: sklearn.model_selection cross validation
        sklearn cross validation method. Must implement split()
    result: sktime result class
        Object for saving the results
    """

    def __init__(self, tasks, datasets, strategies, cv, result):
        self._tasks = tasks
        self._datasets = datasets

        self._validate_strategy_names(strategies)
        self._strategies = strategies

        self._cv = cv
        self._result = result

    def run(self, predict_on_runtime=True, save_strategies=True):
        """
        Method for running the orchestrator
        
        Parameters
        ----------
        predict_on_runtime : bool, optional (default=True)
            If True makes predictions after the estimator is trained
        save_strategies : bool, optional (default=True)
            If True saves the trained strategies on the disk
        """

        for task, data in zip(self._tasks, self._datasets):
            dts_loaded = data.load()
            for strategy in self._strategies:
                for cv_fold, (train, test) in enumerate(self._cv.split(dts_loaded)):

                    strategy.fit(task, dts_loaded.iloc[train])

                    if predict_on_runtime:
                        y_pred = np.array(strategy.predict(dts_loaded.iloc[test]), dtype=np.intp)
                        y_true = np.array(dts_loaded[task.target].iloc[test].values, dtype=np.intp)
                        if hasattr(strategy, 'predict_proba'):
                            actual_probas = strategy.predict_proba(dts_loaded.iloc[test])
                        else:
                            #if no prediction probabilities were given set the probability of the predicted class to 1 and the rest to zeto.
                            num_class_true = np.max(y_true) + 1
                            num_class_pred = np.max(y_pred) + 1
                            num_classes = max(num_class_pred, num_class_true)
                            num_predictions = len(y_pred)
                            actual_probas = (num_predictions, num_classes)
                            actual_probas = np.zeros(actual_probas)
                            actual_probas[np.arange(num_predictions),y_pred] = 1
 
                        self._result.save(dataset_name=data.dataset_name,
                                          strategy_name=strategy.name,
                                          y_true=y_true.tolist(),
                                          y_pred=y_pred.tolist(),
                                          actual_probas=actual_probas,
                                          cv_fold=cv_fold)
                    if save_strategies:
                        strategy.save(dataset_name=data.dataset_name,
                                      cv_fold=cv_fold,
                                      strategies_save_dir=self._result.strategies_save_dir)

    @staticmethod
    def _validate_strategy_names(strategies):
        """
        Validate strategy names
        """

        # Check uniqueness of strategy names
        names = [strategy.name for strategy in strategies]
        if not len(names) == len(set(names)):
            raise ValueError(f"Names of provided strategies are not unique: "
                             f"{names}")

        # Check for conflicts with constructor arguments
        all_params = []
        for strategy in strategies:
            params = list(strategy.get_params(deep=False).keys())
            all_params.extend(params)

        invalid_names = set(names).intersection(set(all_params))
        if invalid_names:
            raise ValueError(f'Strategy names conflict with constructor '
                             f'arguments: {sorted(invalid_names)}')

        # Check for conflicts with double-underscore convention
        invalid_names = [name for name in names if '__' in name]
        if invalid_names:
            raise ValueError(f'Estimator names must not contain __: got '
                             f'{invalid_names}')
