<!--
SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
SPDX-License-Identifier: MPL-2.0
-->

# tine's remote build cache

The [Buck2](https://buck2.build/) build system can ask a remote cache via
[RE-API](https://buck2.build/docs/users/remote_execution/) whether an action has already been run, and
take its outputs instead of running it. tine implements such a cache over an S3 bucket, with properties
that existing implementations such as [bazel-remote](https://github.com/buchgr/bazel-remote) or
[NativeLink](https://github.com/TraceMachina/nativelink) don't provide: not trusting the cloud provider,
being robust against data corruption, reading with plain HTTP/public buckets, and functioning with dumb
bucket expiry rules.

[remote-cache.md](../user/remote-cache.md) documents how to configure and run it.

## Threat model

**The bucket operator is not trusted.** The storage provider, whatever fronts it, and anyone who ever
obtains the write key. Assume they can read, rewrite, delete, reorder and replay any object at will.

**The production build machine is trusted, and can poison the cache.** It runs the actual build actions
and signs their results as well as the uploaded cache entries. A compromised builder can already put
arbitrary code into the produced and published images, and sign them with its keys. The cache adds no
additional exposure. The way to guard against that is keeping image builds reproducible, so that anyone
can rebuild and compare.

**A developer machine is not trusted.** They only get read access and no trusted signing certificate, and
thus cannot poison the cache.

**The local store is trusted no further than the bucket.** Every build machine (of the same user) reaches
its shim, and the store directory is shared, so both are ways for one build to influence the next one. A
shim with an authority therefore serves no unsigned result. Pointers and blobs in the store are verified
exactly as from the bucket. A shim without a signing key also refuses what Buck uploads, since it could
never serve it back. So a store may be shared between checkouts and kept across jobs without becoming a
second, unchecked cache.

**Pull requests are safe only on a hermetic builder.** The action digest covers the command and every
declared input, so a branch that changes any of them builds under a different key and cannot overwrite
what main built. But it can overwrite it through an action that reads something its rule did _not_
declare: another file in the checkout, the network, or even the clock. Such an action produces a
different output under an unchanged key, and a builder running the branch signs and publishes it under
the key main uses. tine's sandbox removes the host and the network from what an action can read, but
binds the whole checkout, so an undeclared project file is not caught. A source build also reads the
files in its source directory that Buck ignores. Buck ignores VCS metadata such as `.git` and every
tracked file listed in `[project] ignore`, and a fresh clone still contains them. The guarantee therefore
requires that every rule declares what it reads, that the builder builds from a fresh clone, and that no
build reads VCS metadata or a file listed in `[project] ignore`. Readers cannot cause any of this, since
they never write.

So a builder with a write key builds trusted branches, and pull requests only after review. Pull requests
from anyone can be built on a builder without a write key: a malicious branch then costs compute, not the
cache.

## Concept

Terms, as RE-API and this document use them:

- **Action**: one command with its inputs. Buck2 hashes both into the SHA-256 **action digest**; any
  changed input is a different action.
- **Result**: what an action produced: a list of its outputs (files, directory listings, stdout,
  stderr), each entry is the output's path and the SHA-256 digest of its bytes. The bytes themselves
  are not in the result; the digest is how they are looked up in the bundles. RE-API calls this an
  [`ActionResult`](https://github.com/bazelbuild/remote-apis/blob/main/build/bazel/remote/execution/v2/remote_execution.proto).
- **Bundle**: one bucket object: a container with one **member** per output (see "Bundle container
  format" below), named by the output's digest and holding its bytes. The bundle is named by the **set
  digest**, the hash of the sorted member digests.
- **Pointer**: the bucket object a reader looks up by action digest. It holds the result and the name of
  its bundle, and is signed by the builder.
- **Shim**: the build cache process on each machine that Buck2 speaks RE-API to. It keeps a local store
  of results and output bytes, and is the only thing that touches the bucket. A **builder** is a shim
  with a write key and a signing key, a **reader** has neither.

Read path:

- Buck2 computes the action digest `d` and asks the shim for its result.
- Shim has the pointer locally: verify it as below, return the result. Failure: drop it, carry on.
- Shim `GET`s the pointer `ac/<d>`. Missing: cache miss. Otherwise verify the builder's signature over `d`
  and the pointer's content, and that the signing certificate (`GET keys/<key id>` when unknown) chains
  to the configured authority. Failure: miss, logged.
- Every output the result names is already local: return the result. Otherwise `GET` the bundle the
  pointer names, in full. Check that its members are exactly the outputs the result names and that each
  matches its digest. Store them locally, return the result. Missing, or any check failing: miss.
- Buck2 reads the outputs it wants from the shim's local store on a hit, or runs the action on a miss.

Two GETs per hit, no HEAD, no LIST, no S3 API.

Write path, builder only:

- Buck2 uploads the output bytes to the shim, which verifies each against its digest and stores it.
- Buck2 hands the shim the result for `d`.
- Shim packs the outputs the result names into a bundle and computes the set digest. A bundle the bucket
  already has gets its date refreshed, otherwise `PUT bundle/<set digest>`. No HEAD first: two builders
  racing on the same bundle write the same bytes.
- Shim signs `d` plus result and bundle name, `PUT`s that as the pointer `ac/<d>`.

Two PUTs per cached action, nothing per output. Writes happen via the S3 API with a write key.

## Bucket layout

- `ac/<action digest>`: the pointers: `signature || { bundle name, result }`
- `bundle/<set digest>`: the bundles
- `keys/<key id>`: the certificate of a signing key

Flat keys under these three prefixes, no sharding (not necessary any more with recent providers). The
read path is identical whether it is served by a bucket's public domain, a static file server, or
something local. Sharding is done on the local file system side, as that works better with Linux file
systems (directory index lookups and `readdir` over hundreds of thousands of entries work poorly).

`<set digest>` is the hash of the **sorted set of member digests**, not of the container bytes; so two
actions with identical outputs share one bundle, a result naming one output under two paths shares it
too, and the container encoding can change without renaming anything. Requirements are documented in
the [user doc's bucket setup](../user/remote-cache.md#the-bucket). A private bucket needs a read
token, and against R2 that means SigV4-signed GETs, since its S3 endpoint has no bearer-token mode. This
is not currently implemented, but can easily be done when needed: reads are done in `Reader.get(key)` in
`bucket.py`, which can be extended.

There is deliberately no buck dep-file cache in the bucket. Its entries are an optimisation whose miss
falls through to the ordinary lookup and then to execution. This is kept local-only, a cold start loses
nothing but a little speed. Keeping them out also removes a forgeable key space: those keys are *not*
action digests, so anything shared would need its own signing rule.

## How this design prevents attacks

| Attack | What stops it |
|---|---|
| Rewrite a member | every member is checked against its own digest |
| Change or add members of a bundle | the members must be exactly what the signed result names |
| Point a result at a different bundle | the pointer is signed, and the bundle name is inside it |
| Move a valid pointer to another action digest | the action digest is in the signed message |
| Strip the signature to look unsigned | a reader holding keys refuses an unsigned object |
| Serve their own certificate and results | the certificate must chain to an authority given out of band |
| Replay an older pointer under its own action | nothing, and nothing needs to: see below |
| Replay a pointer signed by a key since retired | that key's certificate must be valid now |
| Copy a pointer from a bucket sharing the CA | nothing: [one CA per bucket](../user/remote-cache.md#the-keys) |
| Delete anything | nothing. It just costs a rebuild |
| Write a pointer into the local store | it is verified like one from the bucket, on every hit |
| Rewrite a blob file in the local store | it is hashed against its name on every read |
| Upload a result to a reader's shim | refused: without a signing key it could never be served |

The replay row is not a gap. An action digest covers the command and every input, so an older result filed
under it is a result of *the same action*, and serving it is what a cache is for. What replay does buy an
attacker is a result signed by a key that has since been retired, and requiring that key's certificate to
be valid now is what closes it.

The members are also checked to hash to the name the bundle is stored under. That catches a builder that
filed a bundle under the wrong name, not anything an operator could do.

Out of scope: denial of service by an operator who deletes or throttles the bucket, traffic analysis over
which actions a developer looks up, and a compromised builder.

## Bundle granularity

Fetching the whole bundle before answering removes an entire problem class a per-output layout (such as
bazel-remote or NativeLink) has. There is no window between checking that the outputs exist and fetching
them, so nothing can be stale and no reference can dangle: The fetch is the existence check. An age
expiry rule on the bucket cannot break a build: a pointer whose bundle is gone is a miss. And no index
from output digest to bundle is needed for a hit, because after unpacking, the local store *is* the
index.

This is affordable because a bundle is a single action's outputs, not the whole cache. The total may grow
to gigabytes; what matters is the largest single bundle. For that to work well, the build system needs to
limit/split the maximum result size, i.e. opt into caching results like individual rpm/cargo/Go project
builds which are expensive to compute, but have fairly small (MB range) results. It is not advisable to
cache entire disk images, as they take longer to download from a cache than to build on a local disk.

A bucket that answers badly, a 403 or a 502, is an error and a miss, never "not cached", and is asked
again next time. One that cannot be reached at all is left alone for a minute and every lookup meanwhile
is a miss that says so: a laptop behind a captive portal, or a network that drops rather than refuses,
would otherwise wait out a timeout per action.

## Publishing

**Upload bundle before pointer.** A pointer whose bundle is not there yet is the one ordering that can be
observed as a broken hit.

**Refresh instead of HEAD.** Actions whose digests differ but whose outputs do not are the usual case after
a configuration change, so the builder asks the bucket for the bundle before uploading it. The question is
a **server-side copy of the bundle onto itself**: it costs the same as a HEAD and re-dates the object. A
bundle is written once however many results share it. Without a refresh, the most shared bundles would
be the first to expire, out from under pointers written yesterday. Refreshed on every reuse, a bundle is
as old as its newest pointer. The probe and the upload are under one lock per bundle name, because results
that finish together often share a bundle, and a publisher that assumed another would finish could write
its pointer first.

**Expiry** is the bucket's own age rule, whose requirements are in the [user doc's bucket
setup](../user/remote-cache.md#the-bucket). The builder gets told the lifetime (`object_lifetime`), so
that it stops signing before a result would outlive its certificate.

## Bundle container format

Simple concatenation of members: each a fixed header (`HEADER` in `bundle.py`: digest and size) followed
by the bytes. The member name is the output's digest, so the container is self-describing and needs no
manifest inside it. It is not tar because a general archive format has features (sparse members, links,
size fields past the object) which attacks can abuse, and we don't need. The format sits behind a
`pack`/`unpack` pair, and the bundle name covers the member set rather than the bytes, so the container
format can be swapped without renaming anything.

No compression yet. The bundle's encoding is invisible to Buck2, so compressing it is a local decision
about download time against CPU, and nothing measured so far has needed it: the results of actions which
opt into remote caching mostly consist of already compressed rpms, and compressing Go/Rust binaries does
not make enough of a difference yet. If/when compression is desired: zstd inside `pack`/`unpack` is one
flag and no renames. Separately, the shim advertises no RE-API compressor, which keeps Buck2 on plaintext
ByteStream and its 4 MB batch default.

## The local store

Buck2 asks for outputs one **blob** at a time, the bytes of a single output under its digest, often in a
later build than the one that looked up the **Result**. The S3 bucket stores **Bundles**, so something
local has to hold the unpacked bundle members and remember which bundle each came from: the bucket only
knows whole bundles under set digests and cannot answer "give me this blob" at all.

That is a directory, passed to the shim by `tine`, see the [local store user
documentation](../user/remote-cache.md#the-local-store).

Output bytes are files. Bounded by size and evicted least recently used. Losing one costs a bundle fetch.

Everything else is a row in SQLite: pointers, and which bundle carried an output. They are bounded by
count and outlive the bytes they describe, so an evicted output can be fetched again from its bundle when
Buck2 asks for it by digest.

The store holds nothing it would not accept from the bucket. A pointer is kept as the signed bytes the
bucket held and is verified on every hit, the leaf certificates are stored, and a blob is verified on
every read just like one from the bucket. A row that fails is dropped and the bucket asked instead. So a
local entry lives exactly as long as a bucket entry would, while its leaf is valid, and a reader cannot
write to the store if it configures an authority.

One process holds the store at a time, and the store is disposable.

The one gap of the eager fetch: Buck2 may ask for an output by digest in a later build, and if its bundle
has left the bucket by then, that build fails with Buck2's "expired in the RE CAS" error; `buck clean`
recovers. Rarer than with per-output storage, not gone. A hit checks the outputs it names by size rather
than reading them all, so an output rewritten in the store at its own size is found only when Buck2 reads
it, and is fetched from its bundle then, with the same gap.

See `remote_cache/store.py` and its comments for details.

## The signature

Only the pointer is signed; everything else in the bucket is named by its content. Two byte strings:

- **Signed** with Ed25519: `"tine-cache-ac-v1\0" || <action digest, 64 hex> || payload`. This is never
  stored. The reader rebuilds it from the action digest it asked for and the payload it received, so a
  pointer cannot be moved to another action, and the domain prefix keeps the signature from meaning
  anything outside this cache.
- **Stored** as the pointer `ac/<action digest>`:

  ```
  "TINE" | version u8 | algorithm u8 | key id [8] | signature length u16 | signature | payload
  ```

The payload is a small protobuf message: bundle name and serialised result. The key id is the
first 8 bytes of the SHA-256 of the key's DER `SubjectPublicKeyInfo`, which `openssl pkey -pubin -outform
DER | sha256sum` prints, so an unknown one in a log can be looked up. Ed25519 is the only algorithm; the
algorithm byte and the signature length are written anyway so that a future second one is just a new
constant, not a format change.

Two startup checks keep a misconfiguration from becoming a silent downgrade:

- A builder is given the same `authority` certificates as a reader, and refuses to start when its own
  leaf certificate does not chain to one of them, or when it has a signing key and no `authority` at all.
  Either would fill a cache nobody can read.
- A reader refuses to start without an authority. Forgotten keys must never turn into trusting the bucket
  operator. (Note: there is an explicit `unsigned` setting for local testing).

## The keys

Two keys per builder. The **CA key** lives in the build server's TPM and never leaves it; it signs the
leaf certificate, once per rotation. The **leaf key** is an ordinary Ed25519 key file on the same
machine; it signs every pointer. There is no flat list of trusted keys: a reader is given CA certificates
(`authority`) and nothing else. Provisioning and rotating them is described in the [user doc's keys
section](../user/remote-cache.md#the-keys).

**Why two levels.** TPM signing is too slow for hundreds of results per build, and a TPM has no Ed25519.
A leaf key on disk is acceptable because the machine is already trusted to sign releases, and unlike the
CA key the leaf expires. Ed25519 because it is small, fast, and one algorithm means one code path.

**Distribution.** The builder publishes its leaf certificate to `keys/<key id>` at startup. A reader
fetches an unknown key id once, validates the chain and keeps it in its store, so rotating the leaf changes
nothing on any reader, and what the store holds can be verified with the bucket unreachable. A kept
certificate that has expired is fetched once more before a pointer is refused: a renewed certificate keeps
the key and so the key id, and the bucket may hold a newer one.

**What a reader checks on a leaf**: it chains to a configured CA, it is valid now by the reader's own clock
(on every use, not once when fetched), it is not itself a CA, it has `KeyUsage digitalSignature`, and it
holds the key it was filed under. The key id only selects the certificate; the signature is always checked
against the key inside it.

**Lifetimes.** Only the reader's clock bounds a stolen leaf key: a signing time in the payload would be
chosen by whoever holds the key, so none is recorded. A pointer is read for as long as the bucket keeps
it, so the leaf must outlive the last pointer signed under it by the object lifetime: that is the
validity rule when [provisioning a leaf](../user/remote-cache.md#the-keys), and the builder refuses to
sign once less than the object lifetime is left on its certificate. A stolen leaf stays good until its
`notAfter`; the answer is rotating the CA.

**Not here**: no revocation list and no transparency log, rotating the CA is the revocation; no
intermediate CAs, a leaf must be issued directly by a configured authority.

## What Buck2 asks for

Nine RPCs, from its own client (`remote_execution/oss/re_grpc/src/client.rs`): `get_capabilities`,
`get_action_result`, `update_action_result`, `find_missing_blobs`, `batch_update_blobs`,
`batch_read_blobs`, and ByteStream `read`, `write`, `query_write_status`. No Execution service.

Only `get_action_result` and `update_action_result` reach the bucket. `find_missing_blobs`,
`batch_read_blobs` and ByteStream `read` are answered from the local store, ranges included; the bucket is
never asked whether an output exists.

`get_capabilities` is not optional: the client queries it unless a project says otherwise, and reads back
the batch size limit and the compressor list.

Two things about Buck2 that are easy to lose a day to:

- **`-c buck2_re_client.address=...` is ignored.** The address only reaches Buck2 through a config *file*
  read when a daemon starts, and the daemon has to be killed for a change to take effect. `tine buck`
  replaces the daemon whenever the address it wrote changes.
- **The isolation dir is part of every action digest**, because output paths reach the action's command and
  they read `buck-out/<isolation dir>/...`. The default isolation dir is literally `v2`. Two isolation dirs
  never share a cache entry.

Buck2 also stores one extra empty result per cache to check it may write at all
(`buck2_execute_impl/src/executors/empty_action_result.rs`), under a digest built from a compiled-in
command and the platform properties. That probe is the *only* way it decides whether
it may upload; it ignores the `update_enabled` capability which a RE-API server sends.
A shim that refuses uploading would cause `WARN ... Cache upload for <digest> failed` noise on readers
(i.e. developer builds). To avoid that, tell Buck2 via configuration: `tine` writes `[tine] cache-uploads`
from the `[cache]` settings, and `platform/defs.bzl` gates the executor's `allow_cache_uploads` on it.

Buck2 fails an action outright when the cache it was configured with does not answer, rather than
treating it as a miss. So `tine buck` builds the shim with `--no-remote-cache`, in the foreground so that
a first box build lists its actions, and then starts the built shim in the background.

## Running it

One shim per user, started by `tine buck` before a build if nothing is serving that user's store yet,
and exiting on its own after being idle for 15 minutes. The store, the status socket and the port are all
per user for the same reason: a second user's shim must not find the first's port taken and the first's
store locked. A CI runner that is new for every job either keeps the local store on a persistent
volume/directory, or pays the bundle fetches every time.

The shim runs in its own box, `tine//remote_cache:shim.box`, which carries the grpc and cryptography
packages it needs, entered host-integrated so that it sees the store, the key files and the network. Its
unit tests run in that box too, and drive a pinned SeaweedFS for the S3-and-HTTP interop.
