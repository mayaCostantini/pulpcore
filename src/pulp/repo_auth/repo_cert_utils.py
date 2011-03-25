#!/usr/bin/python
#
# Copyright (c) 2010 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
# Red Hat trademarks are not licensed under GPLv2. No permission is
# granted to use or replicate Red Hat trademarks that are incorporated
# in this software or its documentation.

'''
This module contains utilities to support operations around repo cert bundles
(both feed and consumer related). The functionality includes verifying a cert
bundle contains the required pieces, storage and retrieval, and managing the
distinction between feed and consumer bundles.

A cert bundle consists of three pieces:
 * Entitlement certificate that the caller specifies to the destination to
   present its credentials. This is an x509 certificate that is not necessarily
   unique to the consumer, but rather provides access to a repository.
 * Private key for the above x509 certificate.
 * Certificate Authority (CA) certificate. This varies depending on the type
   of cert bundle (feed v. consumer):
   * Feed: This is the CA used to sign the feed server's SSL certificate. It
     will be used to verify that the destination server is actually what Pulp
     expects it to be.
   * Consumer: This is the CA used to sign the entitlement certificate. This is
     used to verify the entitlement cert provided by the consumer wasn't forged.

In the above descriptions, the caller is the component requesting the repo data
(the Pulp server to the repo feed or the consumer) and the destination is the
component serving the data (the feed source or the Pulp server).

A cert bundle is represented by a dict with the following keys. The value at each key
is the PEM encoded contents of the certificate or key.
 * 'ca' - CA certificate
 * 'cert' - Certificate
 * 'key' - Private key for the certificate

The validate_cert_bundle method is used to ensure that only these keys are present
in a cert bundle dict.
'''

import logging
import shutil
from threading import RLock
import os

from M2Crypto import X509

from pulp.server.pexceptions import PulpException
import pulp.server.config as config


# -- constants ----------------------------------------------------------------------------

VALID_BUNDLE_KEYS = ('ca', 'cert', 'key')

# Single write lock for all repos and global; the usage should be infrequent enough
# that it's not an issue.
WRITE_LOCK = RLock()

GLOBAL_BUNDLE_PREFIX = 'pulp-global-repo'

LOG = logging.getLogger(__name__)

# -- public ----------------------------------------------------------------------------

def validate_cert_bundle(bundle):
    '''
    Validates that the given dict contains only the required pieces of a cert bundle.
    See the module level comments for more information on what contents are being
    checked. If the validation fails, an exception will be raised. If the bundle
    is valid, nothing is returned from this call.

    @param bundle: mapping of item to its PEM encoded contents; cannot be None
    @type  bundle: dict {str, str}

    @raises ValueError if the bundle is not a dict with the required keys
    '''
    if bundle is None:
        raise ValueError('Bundle must be specified')

    if type(bundle) != dict:
        raise ValueError('Bundle must be a dict; found [%s]' % type(bundle))

    missing_keys = [k for k in VALID_BUNDLE_KEYS if k not in bundle]
    if len(missing_keys) > 0:
        raise ValueError('Missing items in cert bundle [%s]' % ', '.join(missing_keys))

    extra_keys = [k for k in bundle.keys() if k not in VALID_BUNDLE_KEYS]
    if len(extra_keys) > 0:
        raise ValueError('Unexpected items in cert bundle [%s]' % ', '.join(extra_keys))

def delete_for_repo(repo_id):
    '''
    Deletes all cert bundles for the given repo. If no cert bundles have been
    stored for this repo, this method does nothing (will not throw an error).

    @param repo_id: identifies the repo
    @type  repo_id: str
    '''
    repo_dir = _repo_cert_directory(repo_id)

    if os.path.exists(repo_dir):
        LOG.info('Deleting certificate bundles at [%s]' % repo_dir)
        shutil.rmtree(repo_dir)

def delete_global_cert_bundle():
    '''
    Deletes the global repo certificate bundle. If it does not exist, this call
    has no effect (no error is raised).
    '''
    cert_dir = _global_cert_directory()

    # Currently, _write_cert_bundle appends the suffix of the bundle key to
    # the file when generating it's filename; this method assumes that logic
    for suffix in VALID_BUNDLE_KEYS:
        filename = os.path.join(cert_dir, '%s.%s' % (GLOBAL_BUNDLE_PREFIX, suffix))
        if os.path.exists(filename):
            LOG.info('Deleting global repo cert bundle file [%s]' % filename)
            os.remove(filename)

def read_global_cert_bundle(pieces=VALID_BUNDLE_KEYS):
    '''
    Loads the contents of the global cert bundle. If pieces is specified, only
    the bundle pieces specified will be loaded (must be a subset of VALID_BUNDLE_KEYS).

    @param pieces: list of pieces of the bundle to load in this call; if unspecified,
                   all of the bundle components will be loaded
    @type  pieces: list of str

    @return: mapping of bundle piece to the contents of that bundle item (i.e. the
             PEM encoded certificate, not a filename); returns None if the global
             cert bundle does not exist
    @rtype:  dict {str, str} - keys will be taken from the pieces parameter; None
             is returned if the global cert bundle does not exist
    '''

    cert_dir = _global_cert_directory()

    result = {}
    for suffix in pieces:
        filename = os.path.join(cert_dir, '%s.%s' % (GLOBAL_BUNDLE_PREFIX, suffix))

        if os.path.exists(filename):
            f = open(filename, 'r')
            contents = f.read()
            f.close()
            result[suffix] = contents

    if len(result) == 0:
        return None
    else:
        return result

def write_feed_cert_bundle(repo_id, bundle):
    '''
    Writes the given feed cert bundle to disk.

    See _write_cert_bundle for details on params and return.
    '''
    cert_dir = _repo_cert_directory(repo_id)
    return _write_cert_bundle('feed-%s' % repo_id, cert_dir, bundle)

def write_consumer_cert_bundle(repo_id, bundle):
    '''
    Writes the given consumer cert bundle to disk.

    See _write_cert_bundle for details on params and return.
    '''
    cert_dir = _repo_cert_directory(repo_id)
    return _write_cert_bundle('consumer-%s' % repo_id, cert_dir, bundle)

def write_global_repo_cert_bundle(bundle):
    '''
    Writes the given bundle to the global repo auth location.

    See _write_cert_bundle for details on params and return.
    '''
    cert_dir = _global_cert_directory()
    return _write_cert_bundle(GLOBAL_BUNDLE_PREFIX, cert_dir, bundle)

def validate_certificate(cert_filename, ca_filename):
    '''
    Validates a certificate against a CA certificate.

    @param cert_filename: full path to the PEM encoded certificate to validate
    @type  cert_filename: str

    @param ca_filename: full path to the PEM encoded CA certificate
    @type  ca_filename: str

    @return: true if the certificate was signed by the given CA; false otherwise
    @rtype:  boolean
    '''

    ca = X509.load_cert(ca_filename)
    cert = X509.load_cert(cert_filename)
    return cert.verify(ca.get_pubkey())

def validate_certificate_pem(cert_pem, ca_pem):
    '''
    Validates a certificate against a CA certificate.

    @param cert_pem: PEM encoded certificate
    @type  cert_pem: str

    @param ca_pem: PEM encoded CA certificate
    @type  ca_pem: str

    @return: true if the certificate was signed by the given CA; false otherwise
    @rtype:  boolean
    '''
    ca = X509.load_cert_string(ca_pem)
    cert = X509.load_cert_string(cert_pem)
    return cert.verify(ca.get_pubkey())

# -- private ----------------------------------------------------------------------------
    
def _write_cert_bundle(file_prefix, cert_dir, bundle):
    '''
    Writes the files represented by the cert bundle to a directory on the
    Pulp server unique to the given repo. If certificates already exist in the
    repo's certificate directory, they will be overwritten. The file prefix
    will be used to differentiate between files that belong to the feed
    bundle v. those that belong to the consumer bundle.

    @param file_prefix: used in the filename of the bundle item to differentiate it
                        from other bundles; cannot be None
    @type  file_prefix: str

    @param cert_dir: absolute path to the location in which the cert bundle should
                     be written; cannot be None
    @type  cert_dir: str

    @param bundle: cert bundle (see module docs for more information on format)
    @type  bundle: dict {str, str}

    @raises ValueError: if bundle is invalid (see validate_cert_bundle)

    @return: mapping of cert bundle item (see module docs) to the absolute path
             to where it is stored on disk
    '''

    WRITE_LOCK.acquire()

    try:
        # Create the cert directory if it doesn't exist
        if not os.path.exists(cert_dir):
            os.makedirs(cert_dir)

        # For each item in the cert bundle, save it to disk using the given prefix
        # to identify the type of bundle it belongs to
        cert_files = {}
        for key, value in bundle.items():
            filename = os.path.join(cert_dir, '%s.%s' % (file_prefix, key))
            try:
                LOG.info('Storing repo cert file [%s]' % filename)
                f = open(filename, 'w')
                f.write(value)
                f.close()
                cert_files[key] = str(filename)
            except:
                LOG.exception('Error storing certificate file [%s]' % filename)
                raise PulpException('Error storing certificate file [%s]' % filename)

        return cert_files
    
    finally:
        WRITE_LOCK.release()

def _repo_cert_directory(repo_id):
    '''
    Returns the absolute path to the directory in which certificates for the
    given repo are stored.

    @return: absolute path to a directory that may not exist
    @rtype:  str
    '''
    cert_location = config.config.get('repos', 'cert_location')
    cert_dir = os.path.join(cert_location, repo_id)
    return cert_dir

def _global_cert_directory():
    '''
    Returns the absolute path to the directory in which global repo auth
    credentials are stored.

    @return: absolute path to a directory that may not exist
    @rtype:  str
    '''
    global_cert_location = config.config.get('repos', 'global_cert_location')
    return global_cert_location

# -- classes -----------------------------------------------------------------------

class ProtectedRepoListingFile:

    def __init__(self, filename):
        '''
        @param filename: absolute path to the file; the file does not need to
                         exist at the time of instantiation, the save method will write it
                         out if it doesn't
        @type  filename: string; may not be None

        @raise ValueError: if filename is missing
        '''
        if filename is None:
            raise ValueError('Filename must be specified when creating a ProtectedRepoListingFile')

        self.filename = filename
        self.listings = {} # mapping of relative path to repo ID

    def delete(self):
        '''
        If the repo file exists, it will be deleted. If not, this method does nothing.

        @raise Exception: if there is an error during the delete
        '''
        if os.path.exists(self.filename):
            os.unlink(self.filename)

    def load(self, allow_missing=True):
        '''
        Loads the repo file.

        @param allow_missing: if True, this call will not throw an error if the file cannot
                              be found; defaults to True
        @type  allow_missing: bool

        @raise Exception: if there is an error during the read
        '''
        if allow_missing and not os.path.exists(self.filename):
            return

        f = open(self.filename, 'r')
        contents = f.read()
        f.close()

        # Parse into data structure
        for line in contents.split('\n'):
            pieces = line.split(',')
            self.listings[pieces[0]] = pieces[1]

    def save(self):
        '''
        Saves the current repositories to the repo file.

        @raise Exception: if there is an error during the write
        '''
        f = open(self.filename, 'w')

        for url in self.listings.keys():
            f.write('%s,%s' % (url, self.listings[url]))
        
        f.close()

    # -- contents manipulation ------------------------------------------------------------

    def add_protected_repo_path(self, relative_path_url, repo_id):
        '''
        Adds a new listing of a protected repo. If a listing already exists
        for the given URL, it is overwritten.

        @param relative_path_url: relative path for the repo, used to identify the repo
                                  from a request URL
        @type  relative_path_url: str

        @param repo_id: id of the repo, used to look up the repo credentials
        @type  repo_id: str
        '''
        self.listings[relative_path_url] = repo_id

    def remove_protected_repo_path(self, relative_path_url):
        '''
        Removes the given repo path from the set of protected repos. If the URL
        is not present in the listings, this method has no effect.

        @param relative_path_url: relative path for the repo; this should be the same
                                  as used when the repo path was first added
        @type  relative_path_url: str
        '''
        self.listings.pop(relative_path_url, None) # will not error if key isn't present
