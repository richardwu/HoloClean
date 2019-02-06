import logging
import pandas as pd
import time
from tqdm import tqdm
import itertools
import random
import math

from dataset import AuxTables, CellStatus
from .estimators import Logistic


class DomainEngine:
    def __init__(self, env, dataset, max_sample=5):
        """
        :param env: (dict) contains global settings such as verbose
        :param dataset: (Dataset) current dataset
        :param max_sample: (int) maximum # of domain values from a random sample
        """
        self.env = env
        self.ds = dataset
        self.weak_label_thresh = env["weak_label_thresh"]
        self.domain_thresh_2 = env["domain_thresh_2"]
        self.max_domain = env["max_domain"]
        self.setup_complete = False
        self.active_attributes = None
        self.domain = None
        self.total = None
        self.correlations = None
        self._corr_attrs = {}
        self.cor_strength = env["cor_strength"]
        self.max_sample = max_sample
        self.single_stats = {}
        self.pruned_pair_stats = {}
        self.all_attrs = {}

    def setup(self):
        """
        setup initializes the in-memory and Postgres auxiliary tables (e.g.
        'cell_domain', 'pos_values').
        """
        tic = time.time()
        random.seed(self.env['seed'])
        self.find_correlations()
        self.setup_attributes()
        domain = self.generate_domain()
        self.store_domains(domain)
        status = "DONE with domain preparation."
        toc = time.time()
        return status, toc - tic

    def find_correlations(self):
        """
        find_correlations memoizes to self.correlations; a DataFrame containing
        the pairwise correlations between attributes (values are treated as
        discrete categories).
        """
        df = self.ds.get_raw_data()[self.ds.get_attributes()].copy()
        # Convert dataset to categories/factors.
        for attr in df.columns:
            df[attr] = df[attr].astype('category').cat.codes
        # Drop columns with only one value and tid column.
        df = df.loc[:, (df != 0).any(axis=0)]
        # Compute correlation across attributes.
        m_corr = df.corr()
        self.correlations = m_corr

    def store_domains(self, domain):
        """
        store_domains stores the 'domain' DataFrame as the 'cell_domain'
        auxiliary table as well as generates the 'pos_values' auxiliary table,
        a long-format of the domain values, in Postgres.

        pos_values schema:
            _tid_: entity/tuple ID
            _cid_: cell ID
            _vid_: random variable ID (all cells with more than 1 domain value)
            _

        """
        if domain.empty:
            raise Exception("ERROR: Generated domain is empty.")
        else:
            self.ds.generate_aux_table(AuxTables.cell_domain, domain, store=True, index_attrs=['_vid_'])
            self.ds.aux_table[AuxTables.cell_domain].create_db_index(self.ds.engine, ['_tid_'])
            self.ds.aux_table[AuxTables.cell_domain].create_db_index(self.ds.engine, ['_cid_'])
            query = "SELECT _vid_, _cid_, _tid_, attribute, a.rv_val, a.val_id from %s , unnest(string_to_array(regexp_replace(domain,\'[{\"\"}]\',\'\',\'gi\'),\'|||\')) WITH ORDINALITY a(rv_val,val_id)" % AuxTables.cell_domain.name
            self.ds.generate_aux_table_sql(AuxTables.pos_values, query, index_attrs=['_tid_', 'attribute'])

    def setup_attributes(self):
        self.active_attributes = self.get_active_attributes()
        total, single_stats, pair_stats = self.ds.get_statistics()
        self.total = total
        self.single_stats = single_stats
        logging.debug("preparing pruned co-occurring statistics...")
        tic = time.clock()
        self.pruned_pair_stats, self._temp_stats = self._pruned_pair_stats(pair_stats)
        logging.debug("DONE with pruned co-occurring statistics in %.2f secs", time.clock() - tic)
        self.setup_complete = True

    def _pruned_pair_stats(self, pair_stats):
        """
        _pruned_pair_stats converts 'pair_stats' which is a dictionary mapping
            { attr1 -> { attr2 -> {val1 -> {val2 -> count } } } } where
              <val1>: all possible values for attr1
              <val2>: all values for attr2 that appeared at least once with <val1>
              <count>: frequency (# of entities) where attr1: <val1> AND attr2: <val2>

        to a flattened 4-level dictionary { attr1 -> { attr2 -> { val1 -> [pruned list of val2] } } }
        where the pruned list are all candidate values that are in the top
        :param`domain_top_percentile`% of co-occurring probabilities.
        """

        out = {}
        tempout = {}
        for attr1 in tqdm(pair_stats.keys()):
            out[attr1] = {}
            tempout[attr1] = {}
            for attr2 in pair_stats[attr1].keys():
                out[attr1][attr2] = {}
                tempout[attr1][attr2] = {}
                for val1 in pair_stats[attr1][attr2].keys():
                    denom = self.single_stats[attr1][val1]

                    # We sort our candidate values by largest co-occuring probability.
                    sorted_cands = sorted([(val2, count / denom) for val2, count in pair_stats[attr1][attr2][val1].items()], key=lambda t: t[1], reverse=True)
                    assert(abs(sum(proba for _, proba in sorted_cands) - 1.0) < 1e-6)

                    tempout[attr1][attr2][val1] = sorted_cands

                    # We take the top :param`domain_top_percentile`% of domain values.
                    cum_proba = 0.0
                    top_cdf_cands = []
                    for val, proba in sorted_cands:
                        if cum_proba > self.env['domain_top_percentile']:
                            break
                        top_cdf_cands.append(val)
                        cum_proba += proba
                    out[attr1][attr2][val1] = top_cdf_cands

        # return out
        return out, tempout

    def get_active_attributes(self):
        """
        get_active_attributes returns the attributes to be modeled.
        These attributes correspond only to attributes that contain at least
        one potentially erroneous cell.
        """
        query = 'SELECT DISTINCT attribute as attribute FROM {}'.format(AuxTables.dk_cells.name)
        result = self.ds.engine.execute_query(query)
        if not result:
            raise Exception("No attribute contains erroneous cells.")
        return set(itertools.chain(*result))

    def get_corr_attributes(self, attr, thres):
        """
        get_corr_attributes returns attributes from self.correlations
        that are correlated with attr with magnitude at least self.cor_strength
        (init parameter).

        :param thres: (float) correlation threshold (absolute) for returned attributes.
        """
        # Not memoized: find correlated attributes from correlation dataframe.
        if (attr, thres) not in self._corr_attrs:
            self._corr_attrs[(attr,thres)] = []

            if attr in self.correlations:
                d_temp = self.correlations[attr]
                d_temp = d_temp.abs()
                self._corr_attrs[(attr,thres)] = [rec[0] for rec in d_temp[d_temp > thres].iteritems() if rec[0] != attr]

        return self._corr_attrs[(attr, thres)]

    def generate_domain(self):
        """
        Generates the domain for each cell in the active attributes as well
        as assigns variable IDs (_vid_) (increment key from 0 onwards, depends on
        iteration order of rows/entities in raw data and attributes.

        Note that _vid_ has a 1-1 correspondence with _cid_.

        See get_domain_cell for how the domain is generated from co-occurrence
        and correlated attributes.

        If no values can be found from correlated attributes, return a random
        sample of domain values.

        :return: DataFrame with columns
            _tid_: entity/tuple ID
            _cid_: cell ID (unique for every entity-attribute)
            _vid_: variable ID (1-1 correspondence with _cid_)
            attribute: attribute name
            domain: ||| separated string of domain values
            domain_size: length of domain
            init_value: initial value for this cell
            init_value_idx: domain index of init_value
            fixed: 1 if a random sample was taken since no correlated attributes/top K values
        """

        if not self.setup_complete:
            raise Exception(
                "Call <setup_attributes> to setup active attributes. Error detection should be performed before setup.")

        logging.debug('generating initial set of un-pruned domain values...')
        tic = time.clock()
        # Iterate over dataset rows.
        cells = []
        vid = 0
        records = self.ds.get_raw_data().to_records()
        self.all_attrs = list(records.dtype.names)
        for row in tqdm(list(records)):
            tid = row['_tid_']
            app = []
            for attr in self.active_attributes:
                init_value, dom = self.get_domain_cell(attr, row)
                init_value_idx = dom.index(init_value)
                # We will use an estimator model for additional weak labelling
                # below, which requires an initial pruned domain first.
                weak_label = init_value
                weak_label_idx = init_value_idx
                if len(dom) > 1:
                    cid = self.ds.get_cell_id(tid, attr)
                    app.append({"_tid_": tid,
                                "attribute": attr,
                                "_cid_": cid,
                                "_vid_": vid,
                                "domain": "|||".join(dom),
                                "domain_size": len(dom),
                                "init_value": init_value,
                                "init_index": init_value_idx,
                                "weak_label": weak_label,
                                "weak_label_idx": weak_label_idx,
                                "fixed": CellStatus.NOT_SET.value})
                    vid += 1
                else:
                    add_domain = self.get_random_domain(attr, init_value)
                    # Check if attribute has more than one unique values.
                    if len(add_domain) > 0:
                        dom.extend(self.get_random_domain(attr, init_value))
                        cid = self.ds.get_cell_id(tid, attr)
                        app.append({"_tid_": tid,
                                    "attribute": attr,
                                    "_cid_": cid,
                                    "_vid_": vid,
                                    "domain": "|||".join(dom),
                                    "domain_size": len(dom),
                                    "init_value": init_value,
                                    "init_index": init_value_idx,
                                    "weak_label": init_value,
                                    "weak_label_idx": init_value_idx,
                                    "fixed": CellStatus.SINGLE_VALUE.value})
                        vid += 1
            cells.extend(app)
        domain_df = pd.DataFrame(data=cells).sort_values('_vid_')
        logging.debug('distribution of domain size before estimator:\n%s', domain_df['domain_size'].describe())
        logging.debug('DONE generating initial set of domain values in %.2f', time.clock() - tic)

        # Skip estimator model since we do not require any weak labelling or domain
        # pruning based on posterior probabilities.
        if self.env['weak_label_thresh'] == 1 and self.env['domain_thresh_2'] == 0:
            return domain_df

        # Run pruned domain values from correlated attributes above through
        # posterior model for a naive probability estimation.
        logging.debug('training posterior model for estimating domain value probabilities...')
        tic = time.clock()
        estimator = Logistic(self.env, self.ds, domain_df, self.active_attributes)
        estimator.train(num_epochs=self.env['estimator_epochs'], batch_size=self.env['estimator_batch_size'])
        logging.debug('DONE training posterior model in %.2fs', time.clock() - tic)

        # Predict probabilities for all pruned domain values.
        logging.debug('predicting domain value probabilities from posterior model...')
        tic = time.clock()
        preds_by_cell = estimator.predict_pp_batch()
        logging.debug('DONE predictions in %.2f secs, re-constructing cell domain...', time.clock() - tic)

        logging.debug('re-assembling final cell domain table...')
        tic = time.clock()
        # iterate through raw/current data and generate posterior probabilities for
        # weak labelling
        num_weak_labels = 0
        updated_domain_df = []
        for preds, row in tqdm(zip(preds_by_cell, domain_df.to_records())):
            # no need to modify single value cells
            if row['fixed'] == CellStatus.SINGLE_VALUE.value:
                updated_domain_df.append(row)
                continue

            # prune domain if any of the values are above our domain_thresh_2
            preds = [[val, proba] for val, proba in preds if proba >= self.domain_thresh_2] or preds

            # cap the maximum # of domain values to self.max_domain
            domain_values = [val for val, proba in sorted(preds, key=lambda pred: pred[1], reverse=True)[:self.max_domain]]

            # ensure the initial value is included
            if row['init_value'] not in domain_values:
                domain_values.append(row['init_value'])
            # update our memoized domain values for this row again
            row['domain'] = '|||'.join(domain_values)
            row['domain_size'] = len(domain_values)
            row['weak_label_idx'] = domain_values.index(row['weak_label'])
            row['init_index'] = domain_values.index(row['init_value'])

            # Assign weak label if domain value exceeds our weak label threshold
            weak_label, weak_label_prob = max(preds, key=lambda pred: pred[1])

            if weak_label_prob >= self.weak_label_thresh:
                num_weak_labels+=1

                weak_label_idx = domain_values.index(weak_label)
                row['weak_label'] = weak_label
                row['weak_label_idx'] = weak_label_idx
                row['fixed'] = CellStatus.WEAK_LABEL.value

            updated_domain_df.append(row)

        # update our cell domain df with our new updated domain
        domain_df = pd.DataFrame.from_records(updated_domain_df, columns=updated_domain_df[0].dtype.names).drop('index', axis=1).sort_values('_vid_')
        logging.debug('DONE assembling cell domain table in %.2fs', time.clock() - tic)

        logging.info('number of (additional) weak labels assigned from posterior model: %d', num_weak_labels)

        logging.debug('DONE generating domain and weak labels')
        return domain_df

    def get_domain_cell(self, attr, row):
        """
        get_domain_cell returns a list of all domain values for the given
        entity (row) and attribute.

        We define domain values as values in 'attr' that co-occur with values
        in attributes ('cond_attr') that are correlated with 'attr' at least in
        magnitude of self.cor_strength (init parameter).

        For example:

                cond_attr       |   attr
                H                   B                   <-- current row
                H                   C
                I                   D
                H                   E

        This would produce [B,C,E] as domain values.

        :return: (initial value of entity-attribute, domain values for entity-attribute).
        """

        # Domain maps candidate domain values (str) to their maximum co-occurrence
        # probability across all attributes.
        domain = set([])
        correlated_attributes = self.get_corr_attributes(attr, self.cor_strength)
        # Iterate through all attributes correlated at least self.cor_strength ('cond_attr')
        # and take the top K co-occurrence values for 'attr' with the current
        # row's 'cond_attr' value.
        for cond_attr in correlated_attributes:
            if cond_attr == attr or cond_attr == 'index' or cond_attr == '_tid_':
                continue
            cond_val = row[cond_attr]
            if not pd.isnull(cond_val):
                if not self.pruned_pair_stats[cond_attr][attr]:
                    break
                s = self.pruned_pair_stats[cond_attr][attr]
                try:
                    candidates = s[cond_val]
                    domain.update(candidates)
                except KeyError as missing_val:
                    if not pd.isnull(row[attr]):
                        # Error since co-occurrence must be at least 1 (since
                        # the current row counts as one co-occurrence).
                        logging.error('Missing value: {}'.format(missing_val))
                        raise

        # Remove _nan_ if added due to correlated attributes.
        domain.discard('_nan_')

        # Always include initial value in domain.
        init_value = row[attr]
        domain.update({init_value})

        return init_value, list(domain)

    def get_random_domain(self, attr, cur_value):
        """
        get_random_domain returns a random sample of at most size
        'self.max_sample' of domain values for 'attr' that is NOT 'cur_value'.
        """

        domain_pool = set(self.single_stats[attr].keys())
        domain_pool.discard(cur_value)
        size = len(domain_pool)
        if size > 0:
            k = min(self.max_sample, size)
            additional_values = random.sample(domain_pool, k)
        else:
            additional_values = []
        return additional_values
