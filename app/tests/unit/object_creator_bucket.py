"""A bucket double that holds the staging serving identity to its two grants.

On staging the serving identity has roles/storage.objectCreator and
roles/storage.objectViewer on the cache bucket and nothing else. It can create
an object under a name that is free, read, and list. It cannot replace an
object, because replacing needs storage.objects.delete, so any upload to a name
that already holds an object is refused with 403 whatever its precondition
says. The one open question is what a zero generation create to a taken name
answers: the precondition (412) or the permission (403). The double can answer
either, so a store is proved against both.

Test only hooks plant an object the way an administrator or an older build
would have left it, and run another writer in the middle of an upload, which is
how a race is made deterministic. The store itself can do neither.
"""

from __future__ import annotations

from google.api_core.exceptions import Forbidden, PreconditionFailed


class Blob:
    def __init__(self, bucket, name, generation=None, size=None):
        self.bucket = bucket
        self.name = name
        self.generation = generation
        self.size = size

    def download_as_bytes(self, *, if_generation_match=None, **kwargs):
        current = self.bucket.objects.get(self.name)
        if current is None:
            raise PreconditionFailed("gone")
        data, generation = current
        if if_generation_match is not None and if_generation_match != generation:
            raise PreconditionFailed("generation moved")
        return data

    def upload_from_string(
        self, data, *, content_type=None, if_generation_match=None, **kwargs
    ):
        for index, (matches, callback) in enumerate(self.bucket.before_upload):
            if matches(self.name):
                del self.bucket.before_upload[index]
                callback()
                break
        self.bucket.uploads.append((self.name, if_generation_match))
        if self.name in self.bucket.landed_then_refused:
            # The first request landed and its answer was lost; the client
            # library retried the same bytes and the retry found the name taken.
            self.bucket.landed_then_refused.discard(self.name)
            self.bucket.counter += 1
            self.bucket.objects[self.name] = (bytes(data), self.bucket.counter)
            self.bucket.content_types[self.name] = content_type
        if self.bucket.create_denied:
            raise Forbidden("storage.objects.create is not granted")
        current = self.bucket.objects.get(self.name)
        if current is not None:
            if if_generation_match != 0:
                # A replace, which is what the grants cannot do at all.
                self.bucket.overwrites.append(self.name)
            elif self.bucket.taken == "precondition":
                raise PreconditionFailed("object exists")
            raise Forbidden("storage.objects.delete is not granted")
        if if_generation_match not in (None, 0):
            raise PreconditionFailed("object absent")
        self.bucket.counter += 1
        self.bucket.objects[self.name] = (bytes(data), self.bucket.counter)
        self.bucket.content_types[self.name] = content_type
        self.generation = self.bucket.counter
        self.size = len(data)


class ObjectCreatorBucket:
    name = "listening-post-staging-cache"

    def __init__(self, taken="precondition"):
        assert taken in ("precondition", "forbidden")
        self.taken = taken
        self.objects = {}
        self.uploads = []
        self.overwrites = []
        self.content_types = {}
        self.counter = 100
        self.before_upload = []
        # An identity without objectCreator, to tell a denied create apart
        # from a taken name.
        self.create_denied = False
        self.landed_then_refused = set()
        self.listings = []

    def blob(self, name):
        return Blob(self, name)

    def get_blob(self, name):
        if name not in self.objects:
            return None
        data, generation = self.objects[name]
        return Blob(self, name, generation, len(data))

    def list_blobs(self, *, prefix, max_results=None, delimiter=None):
        self.listings.append(
            {"prefix": prefix, "max_results": max_results, "delimiter": delimiter}
        )
        return [
            self.get_blob(name)
            for name in listed_names(self.objects, prefix, max_results, delimiter)
        ]

    # Test hooks. The store never plants or rewrites an object.
    def plant(self, name, data):
        self.counter += 1
        self.objects[name] = (data, self.counter)


def listed_names(names, prefix, max_results=None, delimiter=None):
    """What a bucket listing returns as blobs, in name order.

    With a delimiter, a name that continues past the prefix into a further
    segment is a prefix entry, not a blob, and iterating the listing does not
    yield it. max_results caps the blobs returned.
    """
    found = [
        name
        for name in sorted(names)
        if name.startswith(prefix)
        and (delimiter is None or delimiter not in name[len(prefix) :])
    ]
    return found if max_results is None else found[:max_results]
