from __future__ import unicode_literals
import array
from collections import defaultdict
import io
import logging
import os
import zipfile

import six
from six.moves.urllib.request import urlretrieve
import torch
from tqdm import tqdm
import tarfile
import numpy as np

#from .utils import reporthook

logger = logging.getLogger(__name__)


def reporthook(t):
    """https://github.com/tqdm/tqdm"""
    last_b = [0]

    def inner(b=1, bsize=1, tsize=None):
        """
        b: int, optionala
        Number of blocks just transferred [default: 1].
        bsize: int, optional
        Size of each block (in tqdm units) [default: 1].
        tsize: int, optional
        Total size (in tqdm units). If [default: None] remains unchanged.
        """
        if tsize is not None:
            t.total = tsize
        t.update((b - last_b[0]) * bsize)
        last_b[0] = b
    return inner

class Vocab(object):
    """Defines a vocabulary object that will be used to numericalize a field.

    Attributes:
        freqs: A collections.Counter object holding the frequencies of tokens
            in the data used to build the Vocab.
        stoi: A collections.defaultdict instance mapping token strings to
            numerical identifiers.
        itos: A list of token strings indexed by their numerical identifiers.
    """
    def __init__(self, counter, max_size=None, min_freq=1, specials=['<pad>'],
                 vectors=None):
        """Create a Vocab object from a collections.Counter.

        Arguments:
            counter: collections.Counter object holding the frequencies of
                each value found in the data.
            max_size: The maximum size of the vocabulary, or None for no
                maximum. Default: None.
            min_freq: The minimum frequency needed to include a token in the
                vocabulary. Values less than 1 will be set to 1. Default: 1.
            specials: The list of special tokens (e.g., padding or eos) that
                will be prepended to the vocabulary in addition to an <unk>
                token. Default: ['<pad>']
            vectors: one of either the available pretrained vectors
                or custom pretrained vectors (see Vocab.load_vectors);
                or a list of aforementioned vectors
        """
        self.freqs = counter.copy()
        min_freq = max(min_freq, 1)
        counter.update(['<unk>'] + specials)

        self.stoi = defaultdict(_default_unk_index)
        self.stoi.update({tok: i for i, tok in
                          enumerate(['<unk>'] + specials)})
        self.itos = ['<unk>'] + specials

        counter.subtract({tok: counter[tok] for tok in ['<unk>'] + specials})
        max_size = None if max_size is None else max_size + len(self.itos)

        # sort by frequency, then alphabetically
        words_and_frequencies = sorted(counter.items(), key=lambda tup: tup[0])
        words_and_frequencies.sort(key=lambda tup: tup[1], reverse=True)

        for word, freq in words_and_frequencies:
            if freq < min_freq or len(self.itos) == max_size:
                break
            self.itos.append(word)
            self.stoi[word] = len(self.itos) - 1

        self.vectors = None
        if vectors is not None:
            self.load_vectors(vectors)

    def __eq__(self, other):
        if self.freqs != other.freqs:
            return False
        if self.stoi != other.stoi:
            return False
        if self.itos != other.itos:
            return False
        if self.vectors != other.vectors:
            return False
        return True

    def __len__(self):
        return len(self.itos)

    def extend(self, v, sort=False):
        words = sorted(v.itos) if sort else v.itos
        for w in words:
            if w not in self.stoi:
                self.itos.append(w)
                self.stoi[w] = len(self.itos) - 1

    def load_vectors(self, vectors):
        """
        Arguments:
            vectors: one of or a list containing instantiations of the
                GloVe, CharNGram, or Vectors classes. Alternatively, one
                of or a list of available pretrained vectors:
                    glove.6B.50d
                    glove.6B.100d
                    glove.6B.200d
                    glove.6B.300d
        """
        if not isinstance(vectors, list):
            vectors = [vectors]
        for idx, vector in enumerate(vectors):
            if isinstance(vector, six.text_type):
                # Convert the string pretrained vector identifier
                # to a Vectors object
                if vector not in pretrained_aliases:
                    raise ValueError(
                        "Got string input vector {}, but allowed pretrained "
                        "vectors are {}".format(
                            vector, list(pretrained_aliases.keys())))
                vectors[idx] = pretrained_aliases[vector]()
            elif not isinstance(vector, Vectors):
                raise ValueError(
                    "Got input vectors of type {}, expected str or "
                    "Vectors object".format(type(vector)))

        tot_dim = sum(v.dim for v in vectors)
        self.vectors = torch.Tensor(len(self), tot_dim)
        for i, token in enumerate(self.itos):
            start_dim = 0
            for v in vectors:
                end_dim = start_dim + v.dim
                self.vectors[i][start_dim:end_dim] = v[token]
                start_dim = end_dim
            assert(start_dim == tot_dim)

    def set_vectors(self, stoi, vectors, dim, unk_init=torch.Tensor.zero_):
        """
        Set the vectors for the Vocab instance from a collection of Tensors.

        Arguments:
            stoi: A dictionary of string to the index of the associated vector
                in the `vectors` input argument.
            vectors: An indexed iterable (or other structure supporting __getitem__) that
                given an input index, returns a FloatTensor representing the vector
                for the token associated with the index. For example,
                vector[stoi["string"]] should return the vector for "string".
            dim: The dimensionality of the vectors.
            unk_init (callback): by default, initialize out-of-vocabulary word vectors
                to zero vectors; can be any function that takes in a Tensor and
                returns a Tensor of the same size. Default: torch.Tensor.zero_
        """
        self.vectors = torch.Tensor(len(self), dim)
        for i, token in enumerate(self.itos):
            wv_index = stoi.get(token, None)
            if wv_index is not None:
                self.vectors[i] = vectors[wv_index]
            else:
                self.vectors[i] = unk_init(self.vectors[i])


class Vectors(object):

    def __init__(self, name, cache='.vector_cache',
                 url=None, unk_init=torch.Tensor.zero_):
        """Arguments:
               name: name of the file that contains the vectors
               cache: directory for cached vectors
               url: url for download if vectors not found in cache
               unk_init (callback): by default, initalize out-of-vocabulary word vectors
                   to zero vectors; can be any function that takes in a Tensor and
                   returns a Tensor of the same size
         """
        self.unk_init = unk_init
        self.cache(name, cache, url=url)

    def __getitem__(self, token):
        if token in self.stoi:
            return self.vectors[self.stoi[token]]
        else:
            return self.unk_init(torch.Tensor(1, self.dim))

    def cache(self, name, cache, url=None):
        path = os.path.join(cache, name)
        #print('path of cache {}'.format(path))
        path_pt = path + '.pt'

        if not os.path.isfile(path_pt):
            """if not os.path.isfile(path) and url:
                logger.info('Downloading vectors from {}'.format(url))
                if not os.path.exists(cache):
                    os.makedirs(cache)
                dest = os.path.join(cache, os.path.basename(url))
                if not os.path.isfile(dest):
                    with tqdm(unit='B', unit_scale=True, miniters=1, desc=dest) as t:
                        urlretrieve(url, dest, reporthook=reporthook(t))
                logger.info('Extracting vectors into {}'.format(cache))
                ext = os.path.splitext(dest)[1][1:]
                if ext == 'zip':
                    with zipfile.ZipFile(dest, "r") as zf:
                        zf.extractall(cache)
                elif ext == 'gz':
                    with tarfile.open(dest, 'r:gz') as tar:
                        tar.extractall(path=cache)
            if not os.path.isfile(path):
                raise RuntimeError('no vectors found at {}'.format(path))"""

            # str call is necessary for Python 2/3 compatibility, since
            # argument must be Python 2 str (Python 3 bytes) or
            # Python 3 str (Python 2 unicode)
            itos, vectors, dim = [], array.array(str('d')), None
            path = "glove.840B.300d.txt" 
            # Try to read the whole file with utf-8 encoding.
            binary_lines = False
            try:
                with io.open(path, encoding="utf8") as f:
                    lines = [line for line in f]
            # If there are malformed lines, read in binary mode
            # and manually decode each word from utf-8
            except:
                logger.warning("Could not read {} as UTF8 file, "
                               "reading file as bytes and skipping "
                               "words with malformed UTF8.".format(path))
                with open(path, 'rb') as f:
                    lines = [line for line in f]
                binary_lines = True

            logger.info("Loading vectors from {}".format(path))
            for line in tqdm(lines, total=len(lines)):
                # Explicitly splitting on " " is important, so we don't
                # get rid of Unicode non-breaking spaces in the vectors.
                entries = line.rstrip().split(" ")
                word, entries = entries[0], entries[1:]
                if dim is None and len(entries) > 1:
                    dim = len(entries)
                elif len(entries) == 1:
                    logger.warning("Skipping token {} with 1-dimensional "
                                   "vector {}; likely a header".format(word, entries))
                    continue
                elif dim != len(entries):
                    raise RuntimeError(
                        "Vector for token {} has {} dimensions, but previously "
                        "read vectors have {} dimensions. All vectors must have "
                        "the same number of dimensions.".format(word, len(entries), dim))

                if binary_lines:
                    try:
                        if isinstance(word, six.binary_type):
                            word = word.decode('utf-8')
                    except:
                        logger.info("Skipping non-UTF8 token {}".format(repr(word)))
                        continue
                vectors.extend(float(x) for x in entries)
                itos.append(word)

            self.itos = itos
            self.stoi = {word: i for i, word in enumerate(itos)}
            self.vectors = torch.Tensor(vectors).view(-1, dim)
            #print('tensor vector: {}'.format(self.vectors))
            self.dim = dim
            logger.info('Saving vectors to {}'.format(path_pt))
            torch.save((self.itos, self.stoi, self.vectors, self.dim), path_pt)
        else:
            logger.info('Loading vectors from {}'.format(path_pt))
            self.itos, self.stoi, self.vectors, self.dim = torch.load(path_pt)


class GloVe(Vectors):
    url = {
        '42B': 'http://nlp.stanford.edu/data/glove.42B.300d.zip',
        '840B': 'http://nlp.stanford.edu/data/glove.840B.300d.zip',
        'twitter.27B': 'http://nlp.stanford.edu/data/glove.twitter.27B.zip',
        '6B': 'http://nlp.stanford.edu/data/glove.6B.zip',
    }

    def __init__(self, name='840B', dim=300, **kwargs):
        url = self.url[name]
        name = 'glove.{}.{}d.txt'.format(name, str(dim))
        super(GloVe, self).__init__(name, url=url, **kwargs)
        print(super(GloVe, self).__init__(name, url=url, **kwargs))




def _default_unk_index():
    return 0


pretrained_aliases = {
    #"glove.6B.50d": lambda: GloVe(name="6B", dim="50"),
    #"glove.6B.100d": lambda: GloVe(name="6B", dim="100"),
    #"glove.6B.200d": lambda: GloVe(name="6B", dim="200"),
    #"glove.6B.300d": lambda: GloVe(name="6B", dim="300")
    "glove.840B.300d": lambda: GloVe(name="840B", dim="300")
}

glove = GloVe(name='840B', dim=300)

print('Loaded {} words'.format(len(glove.itos)))

def get_word(word):
    return glove.vectors[glove.stoi[word]]

def closest(vec, n=10):
    """
    Find the closest words for a given vector
    """
    all_dists = [(w, torch.dist(vec, get_word(w))) for w in glove.itos]
    return sorted(all_dists, key=lambda t: t[1])[:n]

def print_tuples(tuples):
    for tuple in tuples:
        print('(%.4f) %s' % (tuple[1], tuple[0]))

num = int(input("Enter the no. of sentences which you want to add: "))
words=[]
for i in range(num):
    input("Enter your sentence: ")
    words.append(input("Target word: "))
occurence=[]    
unique_words=list(set(words))
for i in range(len(unique_words)):
    occurence.append(words.count(unique_words[i]))

sum = get_word(str.lower(unique_words[0]))*occurence[0]
for i in range(1,len(unique_words)):
    sum += get_word(str.lower(unique_words[i]))*occurence[i]

x=closest(sum/len(words))
print_tuples(x)

intersection = set(closest(get_word(str.lower(words[0]))))
for i in range(num):
    intersection = intersection & set(closest(get_word(str.lower(words[i]))))

print(intersection)
#vector=glove.vectors
#print(vector[0][1])

"""word1 = input("Enter the word which you want to replace: ")
word1_vec=get_word(str.lower(word1))
word1_vec=word1_vec+word1_vec
print(word1_vec)
word2 = input("Enter the word which you want to replace: ")
word2_vec=get_word(str.lower(word2))
#word3 = input("Enter the word which you want to replace: ")
x = closest(get_word(str.lower(word1)))
y = closest(get_word(str.lower(word2)))
#z = closest(get_word(str.lower(word3)))
#print_tuples(x)
#print_tuples(y)
common = set(x).intersection(y)
print(common)"""
