# CERF Bundle Repository Framework

This framework runs a public repository of ROM bundles for the
[CE Runtime Foundation launcher](https://cerf.cx)
([source](https://github.com/gweslab/cerf)).

The framework packs the files of each device into archives. It writes one
`manifest.json` that lists all bundles and the size and SHA-256 hash of each
archive. Then it uploads the archives and the manifest to a Cloudflare R2
bucket.

**Backend:** Cloudflare R2 is the only backend. Uploads use the S3-compatible
API. Downloads use the public URL of the bucket: a custom domain or an
`r2.dev` URL.

| Location | Contents |
|---|---|
| **This git repository** | The scripts and this README. |
| **`data/` (git-ignored)** | The local copy of the dataset: ROM files, one `cerf.json` for each device, and the archives that the build makes. |
| **R2 bucket** | The published dataset: `manifest.json` and one folder of archives for each bundle. |

Git never contains the dataset. `data/` is the local copy, and the R2 bucket
is the remote copy.

---

## Requirements

- Python 3.9 or newer. The scripts use only the standard library.
- A Cloudflare account with R2 enabled.
- Disk space for the ROM files and their archives. The build keeps each
  archive next to its source files. Thus, plan for approximately two times the
  size of the ROM files.

---

## Step 1 — Configure `.r2_token`

`.r2_token` is a `KEY=value` file at the root of the repository. Git ignores
this file. All scripts read their configuration from it. An `R2_*` environment
variable overrides the same key in the file.

Copy the template:

```sh
copy .r2_token.example .r2_token     # Windows
cp .r2_token.example .r2_token       # POSIX
```

### 1.1 Create the bucket and make it public

1. In the Cloudflare dashboard, go to **R2** → **Create bucket**.
2. On the **Settings** tab of the bucket, give the bucket public read access.
   Use one of these two options:
   - **A custom domain**, for example `downloads.example.com`. Use this option
     for production. Only this option makes
     [download analytics](#download-analytics-optional) possible.
   - **The `r2.dev` subdomain.** This option needs no setup, but Cloudflare
     limits its request rate. Use it only for tests.

### 1.2 Create an S3 API token

1. Go to **R2** → **API** → **Manage R2 API Tokens** → **Create API token**.
2. Set the permission to **Object Read & Write**. Limit the token to your
   bucket.
3. Copy the Access Key ID and the Secret Access Key. Cloudflare shows the
   secret only one time.

### 1.3 Write the values

```ini
# The public read URL from step 1.1. This is not the S3 API endpoint.
R2_PUBLIC_BASE_URL=https://downloads.example.com

# The S3 API credentials from step 1.2.
R2_ACCOUNT_ID=0123456789abcdef0123456789abcdef
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=cerf-bundles
```

Optional keys:

| Key | Default | Description |
|---|---|---|
| `R2_PREFIX` | none | A prefix for all object keys. Use it when other data is also in the bucket. Read the caution below. |
| `R2_ENDPOINT` | `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com` | The S3 API endpoint. |
| `R2_REGION` | `auto` | The S3 region. R2 uses `auto`. |
| `ABUSE_EMAIL` | none | The public contact address for complaints about illegal redistribution, DMCA notices, and takedown requests. The build copies it into `manifest.json`. |

> **CAUTION:** If you set `R2_PREFIX`, add the same prefix to
> `R2_PUBLIC_BASE_URL`. The scripts add the prefix only to uploads. For
> example, with `R2_PREFIX=cerf`, set
> `R2_PUBLIC_BASE_URL=https://downloads.example.com/cerf`.

### 1.4 Test the configuration

```sh
python push.py --check
```

This command writes a test object to the bucket. It reads the object through
the S3 API and through `R2_PUBLIC_BASE_URL`. Then it deletes the object.

If the command stops with `public GET probe failed` after `signed_get=ok`, the
bucket is not public. Another possible cause is that `R2_PUBLIC_BASE_URL`
points to a different location. Do step 1.1 again, or correct
`R2_PUBLIC_BASE_URL`.

---

## Step 2 — Make the local folders

### 2a. Continue an existing repository

If the bucket already contains a published dataset, run this command:

```sh
python pull.py
```

This command downloads all archives in the published manifest. It compares
the size and the SHA-256 hash of each archive with the manifest. Then it
extracts the archives into `data/roms/<bundle>/` and writes the `cerf.json` of
each bundle. The result is a full local copy that the build can use.

> **CAUTION:** Publish all local changes before you run `pull.py`. The command
> deletes `data/roms/` and `data/manifest.json` before it downloads.

### 2b. Start a new repository

Make one folder for each device:

```
data/
└── roms/
    ├── cerfos/
    │   ├── cerf.json          # required
    │   └── NK.bin
    └── jornada720/
        ├── cerf.json
        ├── jornada720.bin
        ├── jorn720_eeprom.bin
        └── jlime_cf.img
```

The folder name is the bundle ID. The bundle ID is the `name` of the bundle in
the manifest, and it is part of all public URLs of the bundle. Use a short,
lowercase name that is safe in a URL, for example `jornada720` or `zune_pavo`.

Put all files of the device directly in its folder. Then write `cerf.json`.
This file tells the framework which files to publish.

---

## Step 3 — Write `cerf.json`

Each bundle folder must contain a `cerf.json`. The file has two parts:

1. **The framework keys.** The `rom`, `storage`, and `additional_packages`
   keys tell the framework which files to pack and publish.
2. **All other keys.** The framework does not read or change these keys.

The build copies the full `cerf.json` into `manifest.json` without changes and
in the same key order. The CERF documentation describes the other keys at
<https://cerf.cx/articles/cerf-json>.

This is the minimum `cerf.json`:

```json
{
  "rom": {
    "primary": "NK.bin"
  }
}
```

### Framework keys

| Key | Type | Required | Result |
|---|---|---|---|
| `rom` | object | yes | The build packs the files of all values into `rom.zip`. |
| `rom.primary` | string | yes | The main OS image. |
| `rom.<any name>` | string or list of strings | no | More files for `rom.zip`, for example `bootloader`, `recovery`, or `additional`. |
| `storage` | object | no | The same rules as `rom`, but no key is required. The build also packs these files into `rom.zip`. |
| `additional_packages` | object: category → list of entries | no | Separate downloads, with one archive for each entry. These files are never in `rom.zip`. |
| `additional_packages.<category>[].file` | string | one of `file` or `directory` | A file in the bundle folder. The build packs it alone into `<file>.zip`. |
| `additional_packages.<category>[].directory` | string | one of `file` or `directory` | A folder in the bundle folder. The build packs all its contents into `<directory>.zip`. The paths in the archive are relative to that folder. |

In `rom` and `storage`, you select the key names. The framework uses only the
values. A string value is one file name. A list value is a list of file names.

```json
{
  "rom": { "primary": "NK.bin", "extensions": "EXT.bin" },
  "storage": { "nand": "nand.bin" }
}
```

For this `cerf.json`, `rom.zip` contains `NK.bin`, `EXT.bin`, and `nand.bin`.

**The build packs only the files that you name.** `rom.zip` contains exactly
the files in `rom` and `storage`, at the root of the archive. The build does
not publish other files in the bundle folder. Thus, you can keep work files in
the bundle folder.

The build stops with an error if one of these conditions is not true:

- `rom.primary` is present.
- `rom` and `storage` are objects.
- Each value in `rom` and `storage` is a string or a list of strings.
- Each named file is in the bundle folder.
- Each package source is a plain name, with no `/`, no `..`, and no subpath.
- Each package source occurs only one time across all categories.
- No `file` package also occurs in `rom` or `storage`.
- No package source has the name `rom`. The name `rom.zip` is already in use.

Other keys in `additional_packages`:

- **Category names are free.** The framework does not interpret them, for
  example `compact_flash_cards`, `Files`, or `demos`.
- **Other keys of a package entry go into the manifest with the entry.** For
  example, use a `"name"` key for a readable package name.

### Full example

`data/roms/jornada720/cerf.json` has one main ROM, an EEPROM dump, and a CF
card image as a separate download:

```jsonc
{
  // ---- other keys: copied into the manifest without changes ----
  "meta": {
    "device_name": "HP Jornada 720",
    "os": { "name": "Handheld PC 2000", "ver_major": 3, "ver_minor": 0, "year": 2000 },
    "device_year": 2000,
    "notes": ["JLime Linux: username: root, then do 'startx'"]
  },
  "board": {
    "id": "jornada_720",
    "configurable_screen_width": 640,
    "configurable_screen_height": 240
  },

  // ---- framework keys: select the published files ----
  "rom": {
    "primary": "jornada720.bin",
    "eeprom": "jorn720_eeprom.bin"
  },
  "additional_packages": {
    "compact_flash_cards": [
      { "file": "jlime_cf.img", "name": "JLime Linux" }
    ]
  }
}
```

This bundle has two archives:

- `rom.zip` contains the two ROM files.
- `jlime_cf.img.zip` contains the card image.

No archive contains `cerf.json`. `cerf.json` is only in `manifest.json`. Thus,
a change to `cerf.json` does not cause a new upload of a ROM archive.

---

## Step 4 — Build

```sh
python make_bundles.py
```

For each bundle, the build writes these archives into the bundle folder:

- `rom.zip`, with the files from `rom` and `storage`.
- One `<source>.zip` for each additional package.

Then the build writes a new `data/manifest.json`.

The archives are deterministic. The same source files always give the same
SHA-256 hash. Thus, the publish step can skip the archives that did not change.

The build is incremental. It makes an archive again only when the name, the
size, or the modification time of one of its source files changes. It keeps
this data in a `.build_cache.json` file in each bundle folder.

If you delete or rename a package, the build deletes the old local archive of
that package.

| Flag | Result |
|---|---|
| `<bundle> [<bundle> …]` | Build only these bundles. The other bundles keep their current manifest entries. |
| `--workers N` | The number of parallel build workers. The default is the smaller of the CPU count and the bundle count. Use `1` for one bundle at a time. |
| `--force` | Make all archives again and ignore `.build_cache.json`. |

---

## Step 5 — Publish

```sh
python push.py
```

`push.py` uploads each archive that `data/manifest.json` lists. It uploads
`manifest.json` last. Thus, the published manifest never lists an archive that
is not in the bucket.

The upload is incremental. `push.py` reads the published `manifest.json`. It
skips each archive that has the same hash in both manifests and is still in
the bucket. It always uploads `manifest.json`, because the manifest contains
each `cerf.json`.

Then `push.py` deletes each object under `roms/` that the manifest does not
list. For example, if you delete a bundle folder and run `push.py`, the bundle
disappears from R2. `push.py` does not change objects outside `roms/` and
`manifest.json`, for example `analytics.json`.

| Flag | Result |
|---|---|
| `--dry-run` | Show the number of uploads and deletions. Change nothing. |
| `--force` | Upload all archives again and ignore the hashes. |
| `--check` | Test the configuration and the public read access. Refer to step 1.4. |

> **CAUTION:** Keep each archive smaller than 5 GiB. `push.py` uploads each
> archive in one `PUT` request and keeps the full archive in memory. R2 does
> not accept one request that is larger than approximately 5 GiB. If an archive
> is too large, divide its files across more packages.

### Build and publish with one command

```sh
python orchestrate.py
```

This command runs `make_bundles.py` and then `push.py`. It stops at the first
failure.

---

## The published dataset

The public URLs are relative to `R2_PUBLIC_BASE_URL`:

```
/manifest.json
/roms/<bundle>/rom.zip
/roms/<bundle>/<source>.zip
/analytics.json                  (optional, refer to "Download analytics")
```

`manifest.json` has version `2`. The bundles are in order of name:

```json
{
  "version": 2,
  "abuse_email": "my@email.com",
  "bundles": [
    {
      "name": "jornada720",
      "updated_at": "2026-07-23T15:55:55Z",
      "archive_path": "roms/jornada720/rom.zip",
      "archive_sha256": "40dd80fa8aec6b62c635ea2d11a1f0f3baa8f9e46686159f74b20b29579fc7ae",
      "archive_size": 18409759,
      "unpacked_size": 33554688,
      "additional_packages": {
        "compact_flash_cards": [
          {
            "file": "jlime_cf.img",
            "name": "JLime Linux",
            "archive_path": "roms/jornada720/jlime_cf.img.zip",
            "archive_sha256": "cc91d1c0be55f6c5da0c3eaf615a9b341c4bdd031370ce663052f8c928f2e82d",
            "archive_size": 67296756,
            "unpacked_size": 528482304
          }
        ]
      },
      "cerf_json": { "…": "the full cerf.json of the bundle" }
    }
  ]
}
```

The fields of each bundle:

- `archive_path`, `archive_sha256`, and `archive_size` give the path, the hash,
  and the size of `rom.zip`.
- `unpacked_size` is the total size of the files in the archive before
  compression. A client can use it to find the necessary free disk space before
  the download.
- `additional_packages` has the same structure as in `cerf.json`. Each entry
  also has the archive fields. The value is `null` if the bundle has no
  packages.
- `updated_at` changes only when an archive, a package, or `cerf.json` changes.
- `cerf_json` is the full `cerf.json` of the bundle.

---

## Download analytics (optional)

`analytics.py` makes a ranking of the most downloaded ROMs from real traffic.
It publishes this ranking next to the dataset.

`analytics.py` needs a Cloudflare custom domain for the bucket. Only this
configuration records the number of downloads for each URL.

Add these keys to `.r2_token`:

```ini
# A Cloudflare API token with the permission Zone → Analytics → Read for the
# download zone, and the ID of that zone (zone Overview page, right sidebar).
CF_API_TOKEN=
CF_ZONE_ID=
```

Each run has two phases:

1. **Private collection.** The script reads the number of `rom.zip` downloads
   (HTTP 200 and 206) from the Cloudflare GraphQL Analytics API. Cloudflare
   keeps this data for approximately 8 days. Thus, each run reads the last days
   again. It stores one record for each UTC day in the local file
   `analytics_db.json`. With this file, the ranking window can be longer than
   8 days. If one run does not occur, the next run adds the missing days.
2. **Public publication.** The script ranks the bundles over a trailing window.
   It uploads `analytics.json` next to `manifest.json`, with the same R2
   credentials as `push.py`.

The format of `analytics.json`:

```json
{ "per_rom": [ { "name": "jornada720", "place": 1 }, { "name": "cerfos", "place": 2 } ] }
```

> **CAUTION:** Do not delete `analytics_db.json`. Git ignores this file. It is
> the only copy of the download data that is older than approximately 8 days.

```sh
python analytics.py                 # collect and publish: the daily command
python analytics.py --dry-run       # collect and show the ranking, write and upload nothing
python analytics.py --collect-only  # update analytics_db.json only
python analytics.py --check         # test the Cloudflare and R2 configuration
python analytics.py --window-days 30 --fetch-days 7
```

Run `analytics.py` one time each day, for example with cron. The count
includes only downloads through the public custom domain. Downloads through the
S3 API are not in the count.
