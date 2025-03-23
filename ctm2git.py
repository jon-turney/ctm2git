#!/usr/bin/env python3

import argparse
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import xtarfile

import calm.version

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable):
        return iterable


CACHE_DIR = '/tmp/ctm2git/cache'
REMOVE_EXTS = [
    '.tar.gz', '.tgz',
    '.tar.bz2', '.tbz',
    '.tar.lzma',
    '.tar.xz', '.txz',
    '.tar.zst',
    '.cpio.gz',
    '.sig',
]
DEFAULT_AUTHOR = 'unknown <unknown@unknown.invalid>'


def url_retrieve_cached(u):
    cache_fn = os.path.join(CACHE_DIR, u.replace('http://', '').replace(os.path.sep, '_'))

    if not os.path.isfile(cache_fn):
        (filename, headers) = urllib.request.urlretrieve(u, cache_fn)
        print('fetching %s' % u, file=sys.stderr)
    else:
        filename = cache_fn
        # print('%s from cache' % filename, file=sys.stderr)

    return filename


class source:
    def __init__(self, url, author):
        self.url = url
        self.author = author


def ctm_to_sourcelist(args):
    if args.arch == 'x86':
        index_url = "http://ctm.crouchingtigerhiddenfruitbat.org/pub/cygwin/circa/index.html"
    else:
        index_url = "http://ctm.crouchingtigerhiddenfruitbat.org/pub/cygwin/circa/64bit/index.html"

    # read index, build list of setup.uni URLs
    urls = []
    html = urllib.request.urlopen(index_url).read().decode()
    for l in html.splitlines():
        m = re.search('<td>(http.*)</td>', l)
        if m:
            urls.append(m.group(1) + '/setup.ini')

    # for each setup.ini URL, fetch it and parse details for selected package
    sources = {}
    for u in tqdm(urls):
        circa = re.search('^(.*)/setup.ini$', u).group(1)
        filename = url_retrieve_cached(u)

        # parse it
        with open(filename, errors='ignore') as f:
            s = parse_setup_ini(f.read(), args.package[0])
            for v in s:
                # because we circas are ordered newest to oldest, data from the
                # oldest circa to contain a version overwrites that from all
                # newer circas
                if (args.since is None) or (calm.version.SetupVersion(v) > calm.version.SetupVersion(args.since[0])):
                    sources[v] = circa + '/' + s[v]

    # show versions and sources
    for v in sorted(sources.keys(), key=calm.version.SetupVersion):
        print(v, sources[v], DEFAULT_AUTHOR)


def sourcelist_to_repo(args):
    package = args.package[0]

    # read from provided filename
    sources = {}
    with open(args.sourcelist[0]) as f:
        for l in f.readlines():
            l = l.strip()
            (v, url, author) = l.split(sep=None, maxsplit=2)
            if author == DEFAULT_AUTHOR:
                print('Unknown author still in sourcelist', file=sys.stderr)
                exit(1)
            sources[v] = source(url, author)

    if not args.since:
        # abort if working directory is already a git repo
        if os.path.exists('.git'):
            print('Working directory is already a git repo', file=sys.stderr)
            exit(1)

        # abort if working directory isn't empty
        if len(os.listdir('.')) != 0:
            print("Working directory isn't empty", file=sys.stderr)
            exit(1)

        subprocess.check_call(['git', 'init', '--initial-branch=master'])

    # for each unique source...
    for v in sources:
        # fetch it
        url = sources[v].url
        author = sources[v].author
        filename = url_retrieve_cached(url)

        # clean working directory
        with os.scandir('.') as entries:
            for entry in entries:
                if entry.path == './.git':
                    continue
                elif entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry.path)
                else:
                    os.remove(entry.path)

        # Look inside source package archive to see if filenames start with a
        # directory with a name ending with '.src' (as current versions of
        # cygport make) ...
        with xtarfile.open(filename, mode='r') as archive:
            strip = any(re.match(r'[^/]*\.src/', f) for f in archive.getnames())

        # ... if so, use --strip-components to trim that
        extra_args = ''
        if strip:
            extra_args = '--strip-components=1'

        # unpack it
        subprocess.check_call(['tar', '-x', extra_args, '-f', filename])

        # remove upstream archives and .sig files
        with os.scandir('.') as entries:
            for entry in entries:
                if any(entry.path.endswith(ext) for ext in REMOVE_EXTS):
                    os.remove(entry.path)

        # if the unarchived upstream source is included in a g-b-s package
        for gbs_src_dir in [(package + '-' + v), (package + '-' + v.rsplit('-')[0])]:
            if os.path.isdir(gbs_src_dir):
                mknetrel = os.path.join(gbs_src_dir, package + '.mknetrel')
                if os.path.exists(mknetrel):
                    os.rename(mknetrel, os.path.join(package + '.mknetrel'))

                cygwin_patches = os.path.join(gbs_src_dir, 'CYGWIN-PATCHES')
                if os.path.exists(cygwin_patches):
                    with os.scandir(cygwin_patches) as patches:
                        for patch in patches:
                            os.rename(patch, os.path.basename(patch))

                shutil.rmtree(gbs_src_dir)

        # avoid trying to make empty commits for very old source packages with
        # which we can do nothing useful
        if len(list(os.scandir('.'))) <= 1:
            if not args.allow_empty:
                continue

        # create a git commit
        subprocess.check_call(['git', 'add', '--all', '-f', '.'])
        circa = re.search(r'circa/(?:64bit/|)([\d/]*)/', url).group(1)
        date = circa + ' UTC'

        env = os.environ.copy()
        env['GIT_COMMITTER_NAME'] = re.search(r'^(.*) <', author).group(1)
        env['GIT_COMMITTER_EMAIL'] = re.search(r'<(.*)>', author).group(1)
        env['GIT_COMMITTER_DATE'] = date

        message = '%s %s\n\nctm2git-circa: %s' % (package, v, circa)
        cmd = ['git', 'commit', '--author', author, '--date=%s' % date, '-m', message]
        if args.allow_empty:
            cmd.append('--allow-empty')
        subprocess.check_call(cmd, env=env)


def parse_setup_ini(contents, package):
    parsed = {}

    for l in contents.splitlines():
        if l.startswith('@'):
            p = l[2:]
        elif l.startswith('version:'):
            v = l[9:]
        elif l.startswith('source:'):
            s = l[8:].split()[0]
            # this extracts the URL from the source: line for all version: lines
            # for the specified package
            if p == package:
                parsed[v] = s

    return parsed


parser = argparse.ArgumentParser(description='Make a git repository from CTM package history')
parser.add_argument('package', action='store', nargs=1)
parser.add_argument('--arch', action='store', required=True, choices=['x86', 'x86_64'])
parser.add_argument('--sourcelist', action='store', nargs=1, help='sourcelist from a previous run')
parser.add_argument('--allow-empty', action='store_true', help='allow empty commits')
parser.add_argument('--since', nargs=1, action='store', help='append versions after SINCE')

(args) = parser.parse_args()
if not args.sourcelist:
    ctm_to_sourcelist(args)
else:
    sourcelist_to_repo(args)
