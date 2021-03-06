# -*- coding: utf-8
# aggregate_dask.py
import argparse
import datetime as dt
import os
import shutil
from collections import Counter

import dask
import dask.dataframe as dd
import s3fs
import simplejson as json
import pandas as pd
from dask.distributed import Client, LocalCluster

from daskvsspark.common import *

INPUT_ROOT = './events'
OUTPUT_ROOT = './aggs_dask'

INPUT_TEMPLATE = '{root}/{event_count}-{nfiles}/*/*/*/*/*/*.parquet'
OUTPUT_TEMPLATE = '{root}/{event_count}-{nfiles}/*.json'


def read_data(read_path):
    """Reads the original Parquet data.
    :returns: DataFrame
    """
    df = dd.read_parquet(read_path).drop('hour', axis=1)
    return df


def counter_chunk(ser):
    """Return counter of values in series."""
    return list(Counter(ser.values).items())


def counter_agg(chunks):
    """Add all counters together and return dict items."""
    total = Counter()
    for chunk in chunks:
        current = Counter(dict(chunk))
        total += current
    return list(total.items())


def nunique_chunk(ser):
    """Get all unique values in series."""
    return ser.unique()


def nunique_agg(chunks):
    """Return number of unique values in all chunks."""
    total = pd.Series()
    for chunk in chunks:
        current = pd.Series(chunk)
        total = total.append(current)
        total = total.drop_duplicates()
    res = total.nunique()
    return res


def group_data(df):
    """Aggregate the DataFrame and return the grouped DataFrame.

    :param df: DataFrame
    :returns: DataFrame
    """
    # round timestamps down to an hour
    df['ts'] = df['ts'].dt.floor('1H')

    # group on customer, timestamp (rounded) and url
    gb = df.groupby(['customer', 'url', 'ts'])

    counter = dd.Aggregation(
        'counter',
        lambda s: s.apply(counter_chunk),
        lambda s: s.apply(counter_agg),
    )

    count_unique = dd.Aggregation(
        'count_unique',
        lambda s: s.apply(nunique_chunk),
        lambda s: s.apply(nunique_agg)
    )

    ag = gb.agg({
        'session_id': [count_unique, 'count'],
        'referrer': counter}
    )

    ag = ag.reset_index()

    # get rid of multilevel columns
    ag.columns = ['customer', 'url', 'ts', 'visitors', 'page_views', 'referrers']
    ag = ag.repartition(npartitions=df.npartitions)

    return ag


def transform_one(ser):
    """Takes a Series object representing a grouped DataFrame row,
    and returns a dict ready to be stored as JSON.

    :returns: pd.Series
    """
    data = ser.to_dict()
    if not data:
        return pd.Series([], name='data')
    page_views = data.pop('page_views')
    visitors = data.pop('visitors')
    data.update({
        '_id': format_id(data['customer'], data['url'], data['ts']),
        'ts': data['ts'].strftime('%Y-%m-%dT%H:%M:%S'),
        'metrics': format_metrics(page_views, visitors),
        'referrers': dict(data['referrers'])
    })
    return pd.Series([data], name='data')


def transform_data(ag):
    """Accepts a Dask DataFrame and returns a Dask Bag, where each record is
    a string, and the contents of the string is a JSON representation of the
    document to be written.

    :param ag: DataFrame
    :returns: DataFrame with one column "data" containing a dict.
    """
    tr = ag.apply(transform_one, axis=1, meta={'data': str})
    tr = tr.repartition(npartitions=ag.npartitions)
    return tr


def delete_path(path):
    """Recursively delete a path and everything under it."""
    if path.startswith('s3://'):
        s3 = s3fs.S3FileSystem()
        if s3.exists(path):
            s3.rm(path)
    elif os.path.exists(path):
        shutil.rmtree(path)


def create_path(path):
    """Create root dir."""
    if not path.startswith('s3://') and not os.path.exists(path):
        os.makedirs(path)


def save_json(tr, path):
    """Write records as json."""
    root_dir = os.path.dirname(path)

    # cleanup before writing
    delete_path(root_dir)
    create_path(root_dir)

    (tr.to_bag()
       .map(lambda t: t[0])
       .map(json.dumps)
       .to_textfiles(path))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--count', type=int, default=100)
    parser.add_argument('--nfiles', type=int, default=24)
    parser.add_argument('--wait', action='store_true', default=False)
    parser.add_argument('--scheduler', choices=['thread', 'process', 'default', 'single'],
                        default='default')
    parser.add_argument('--verbose', action='store_true', default=False)
    parser.add_argument('--address', help='Scheduler address')
    parser.add_argument('--input', default=INPUT_ROOT)
    parser.add_argument('--output', default=OUTPUT_ROOT)
    myargs = parser.parse_args()

    read_path = INPUT_TEMPLATE.format(root=myargs.input, event_count=myargs.count,
                                      nfiles=myargs.nfiles)
    write_path = OUTPUT_TEMPLATE.format(root=myargs.output, event_count=myargs.count,
                                        nfiles=myargs.nfiles)

    set_display_options()
    started = dt.datetime.utcnow()
    if myargs.scheduler != 'default':
        print('Scheduler: {}.'.format(myargs.scheduler))
        getters = {'process': dask.multiprocessing.get,
                   'thread': dask.threaded.get,
                   'single': dask.get}
        dask.set_options(get=getters[myargs.scheduler])

    try:
        if myargs.address:
            # explicit address is a workaround for "Worker failed to start":
            # scheduler and worker have to be started in console.
            # see https://github.com/dask/distributed/issues/1825
            cluster = myargs.address
        else:
            cluster = LocalCluster()

        if myargs.verbose:
            client = Client(address=cluster, silence_logs=False)
        else:
            client = Client(address=cluster)

        df = read_data(read_path)
        aggregated = group_data(df)
        prepared = transform_data(aggregated)
        save_json(prepared, write_path)
        elapsed = dt.datetime.utcnow() - started
        parts_per_hour = int(myargs.nfiles / 24)
        print('{:,} records, {} files ({} per hour): done in {}.'.format(
            myargs.count, myargs.nfiles, parts_per_hour, elapsed))
        if myargs.wait:
            input('Press any key')
    except:
        elapsed = dt.datetime.utcnow() - started
        print('Failed in {}.'.format(elapsed))
        raise
