"""Driver code for the price script.
"""
__author__ = "Martin Blais <blais@furius.ca>"

import csv
import collections
import datetime
import io
import functools
import threading
from os import path
import shelve
import tempfile
import re
import hashlib
import os
import sys
from urllib import parse
from urllib import request
import urllib.parse
import hashlib
import argparse
import logging
from concurrent import futures

from dateutil.parser import parse as parse_datetime

import beancount.prices
from beancount import loader
from beancount.core import data
from beancount.core import amount
from beancount.ops import holdings
from beancount.parser import printer
from beancount.utils import net_utils
from beancount.utils import memo
from beancount.prices import find_prices


# Stand-in currency name for unknown currencies.
UNKNOWN_CURRENCY = '?'


# A cache for the prices.
_cache = None

# Expiration for latest prices in the cache.
DEFAULT_EXPIRATION = datetime.timedelta(seconds=30*60)  # 30 mins.


def now():
    "Indirection in order to be able to mock it out in the tests."
    return datetime.datetime.now()


def fetch_cached_price(source, symbol, date):
    """Call Source to fetch a price, but look and/or update the cache first.

    This function entirely deals with caching and correct expiration. It keeps
    old prices if they were fetched in the past, and it quickly expires
    intra-day prices if they are fetched on the same day.

    Args:
      source: A Python module object.
      symbol: A string, the ticker to fetch.
      date: A datetime.date instance, None if we're to fetch the latest date.
    Returns:
      A SourcePrice instance.
    """
    time_now = now()
    if _cache is None:
        # The cache is disabled; just call and return.
        result = (source.get_latest_price(symbol)
                  if date is None else
                  source.get_historical_price(symbol, date))
    elif date is None or date >= time_now.date():
        # The cache is enabled and we have to compute the current/latest price.
        # Fetch from the cache but miss if the price is too old.
        md5 = hashlib.md5()
        md5.update(str((type(source).__module__, symbol)).encode('utf-8'))
        key = md5.hexdigest()
        try:
            time_created, result = _cache[key]
            if (time_now - time_created) > _cache.expiration:
                raise KeyError
        except KeyError:
            result = source.get_latest_price(symbol)
            _cache[key] = (time_now, result)
    else:
        # The cache is enabled and we are asked to provide an old price. Assume
        # it doesn't change and return the cached value if at all available.
        md5 = hashlib.md5()
        md5.update(str((source.__file__, symbol, date)).encode('utf-8'))
        key = md5.hexdigest()
        try:
            _, result = _cache[key]
        except KeyError:
            result = source.get_historical_price(symbol, date)
            _cache[key] = (None, result)
    return result


def setup_cache(cache_filename, clear_cache):
    """Setup the results cache.

    Args:
      cache_filename: A string or None, the filename for the cache.
      clear_cache: A boolean, if true, delete the cache before beginning.
    """
    if clear_cache and cache_filename and path.exists(cache_filename):
        logging.info("Clearing cache %s", cache_filename)
        os.remove(cache_filename)

    if cache_filename:
        logging.info('Using price cache at "{}" (with indefinite expiration)'.format(
            cache_filename))

        global _cache
        _cache = shelve.open(cache_filename)
        _cache.expiration = DEFAULT_EXPIRATION
        _cache.lock = threading.Lock()  # Note: 'shelve' is not thread-safe by itself.


def reset_cache():
    """Reset the cache to its uninitialized state."""
    global _cache
    _cache = None


def fetch_price(dprice):
    """Fetch a price for the DatePrice job.

    Args:
      dprice: A DatedPrice instances.
      source_map: A mapping of source string to a source module object.
    Returns:
      A list of Price entries corresponding to the outputs of the jobs processed.
    """
    for psource in dprice.sources:
        source = psource.module.Source()
        srcprice = fetch_cached_price(source, psource.symbol, dprice.date)
        if srcprice is not None:
            break
    else:
        if dprice.sources:
            logging.error("Could not fetch for job: %s", dprice)
        return None

    # Invert the currencies if the rate if the rate is inverted.
    base, quote = dprice.base, dprice.quote or srcprice.quote_currency
    if psource.invert:
        base, quote = quote, base

    assert base is not None
    fileloc = data.new_metadata('<{}>'.format(type(psource.module).__name__), 0)
    return data.Price(fileloc, srcprice.time.date(), base,
                      amount.Amount(srcprice.price, quote or UNKNOWN_CURRENCY))


def filter_redundant_prices(price_entries, existing_entries, diffs=False):
    """Filter out new entries that are redundant from an existing set.

    If the price differs, we override it with the new entry only on demand. This
    is because this would create conflict with existing price entries when
    parsing, if the new entries are simply inserted into the input.

    Args:
      price_entries: A list of newly created, proposed to be added Price directives.
      existing_entries: A list of existing entries we are proposing to add to.
      diffs: A boolean, true if we should output differing price entries
        at the same date.
    Returns:
      A filtered list of remaining entries, and a list of ignored entries.
    """
    # Note: We have to be careful with the dates, because requesting the latest
    # price for a date may yield the price at a previous date. Clobber needs to
    # take this into account. See {1cfa25e37fc1}.
    existing_prices = {(entry.date, entry.currency): entry
                       for entry in existing_entries
                       if isinstance(entry, data.Price)}
    filtered_prices = []
    ignored_prices = []
    for entry in price_entries:
        key = (entry.date, entry.currency)
        if key in existing_prices:
            if diffs:
                existing_entry = existing_prices[key]
                if existing_entry.amount == entry.amount:
                    output = ignored_prices
            else:
                output = ignored_prices
        else:
            output = filtered_prices
        output.append(entry)
    return filtered_prices, ignored_prices


def process_args():
    """Process the arguments. This also initializes the logging module.

    Returns:
      A tuple of:
        args: The argparse receiver of command-line arguments.
        jobs: A list of DatedPrice job objects.
        entries: A list of all the parsed entries.
    """
    parser = argparse.ArgumentParser(description=beancount.prices.__doc__.splitlines()[0])

    # Input sources or filenames.
    parser.add_argument('sources', nargs='+', help=(
        'A list of filenames (or source "module/symbol", if -e is '
        'specified) from which to create a list of jobs.'))

    parser.add_argument('-e', '--expressions', '--expression', action='store_true', help=(
        'Interpret the arguments as "module/symbol" source strings.'))

    # Regular options.
    parser.add_argument('-v', '--verbose', action='count', help=(
        "Print out progress log. Specify twice for debugging info."))

    parse_date = lambda s: parse_datetime(s).date()
    parser.add_argument('-d', '--date', action='store', type=parse_date, help=(
        "Specify the date for which to fetch the prices."))

    parser.add_argument('-s', '--swap-inverted', action='store_true', help=(
        "For inverted sources, swap currencies instead of inverting the rate. "
        "For example, if fetching the rate for CAD from 'USD:google/^CURRENCY:USDCAD' "
        "results in 1.25, by default we would output \"price CAD  0.8000 USD\". "
        "Using this option we would instead output \" price USD   1.2500 CAD\"."))

    parser.add_argument('-i', '--inactive', action='store_true', help=(
        "Select all commodities from input files, not just the ones active on the date"))

    parser.add_argument('-u', '--undeclared', action='store_true', help=(
        "Include commodities viewed in the file even without a "
        "corresponding Commodity directive. The currency name itself is "
        "used as the lookup symbol in the default sources."))

    parser.add_argument('-c', '--clobber', action='store_true', help=(
        "Do not skip prices which are already present in input files; fetch them anyway."))

    parser.add_argument('-a', '--all', action='store_true', help=(
        "A shorthand for --inactive, --undeclared, --clobber."))

    parser.add_argument('-n', '--dry-run', action='store_true', help=(
        "Don't actually fetch the prices, just print the list of the ones to be fetched."))

    # Caching options.
    cache_group = parser.add_argument_group('cache')
    cache_filename = path.join(tempfile.gettempdir(),
                               "{}.cache".format(path.basename(sys.argv[0])))
    cache_group.add_argument('--cache', dest='cache_filename',
                             action='store', default=cache_filename,
                             help="Enable the cache and with the given cache name.")
    cache_group.add_argument('--no-cache', dest='cache_filename',
                             action='store_const', const=None,
                             help="Disable the price cache.")

    cache_group.add_argument('--clear-cache', action='store_true',
                             help="Clear the cache prior to startup")

    args = parser.parse_args()

    verbose_levels = {None: logging.WARN,
                      0: logging.WARN,
                      1: logging.INFO,
                      2: logging.DEBUG}
    logging.basicConfig(level=verbose_levels[args.verbose],
                        format='%(levelname)-8s: %(message)s')

    if args.all:
        args.inactive = args.undeclared = args.clobber = True

    # Setup for processing.
    setup_cache(args.cache_filename, args.clear_cache)

    # Get the list of DatedPrice jobs to get from the arguments.
    logging.info("Processing at date: %s", args.date or datetime.date.today())
    jobs = []
    all_entries = []
    if args.expressions:
        # Interpret the arguments as price sources.
        for source_str in args.sources:
            psources = []
            try:
                psource_map = find_prices.parse_source_map(source_str)
            except ValueError:
                if path.exists(source_str):
                    msg = 'Invalid source "{}"; did you provide a filename?'
                else:
                    msg = 'Invalid source "{}"'
                parser.error(msg.format(source_str))
            else:
                for currency, psources in psource_map.items():
                    jobs.append(find_prices.DatedPrice(
                        psources[0].symbol, currency, args.date, psources))
    else:
        # Interpret the arguments as Beancount input filenames.
        for filename in args.sources:
            if not path.exists(filename) or not path.isfile(filename):
                parser.error('File does not exist: "{}"; '
                             'did you mean to use -e?'.format(filename))
                continue
            logging.info('Loading "%s"', filename)
            entries, errors, options_map = loader.load_file(filename, log_errors=sys.stderr)
            jobs.extend(
                find_prices.get_price_jobs_at_date(
                    entries, args.date, args.inactive, args.undeclared))
            all_entries.extend(entries)

    return args, jobs, data.sorted(all_entries)


def main():
    args, jobs, entries = process_args()

    # If we're just being asked to list the jobs, do this here.
    if args.dry_run:
        for dprice in jobs:
            print(find_prices.format_dated_price_str(dprice))
        return

    # Fetch all the required prices, processing all the jobs.
    executor = futures.ThreadPoolExecutor(max_workers=3)
    price_entries = sorted(filter(None, executor.map(fetch_price, jobs)))

    # Avoid clobber, remove redundant entries.
    if not args.clobber:
        price_entries, ignored_entries = filter_redundant_prices(price_entries, entries)
        for entry in ignored_entries:
            logging.info("Ignored to avoid clobber: %s %s", entry.date, entry.currency)

    # Print out the entries.
    printer.print_entries(price_entries)
