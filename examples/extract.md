# Extract `combined.txt`

```sh
awk '/^==== FILE: /{f=$0;sub(/^==== FILE: /,"",f);sub(/ ====$/,"",f);n=split(f,a,"/");d="";for(i=1;i<n;i++)d=d a[i]"/";if(d)system("mkdir -p \""d"\"");next}{print>f}' combined.txt
```

Run it from an empty directory — it writes into the current directory and
recreates subdirectories as it goes.

## Regenerate

```sh
cd splunk-cluster-aws-s3
find . -type f -exec sh -c 'printf "==== FILE: %s ====\n" "$1"; cat "$1"; printf "\n"' _ {} \; > ../combined.txt
```

The output **must** land outside the directory being walked. Redirection creates
the file before `find` runs, so `> combined.txt` inside the tree makes `find`
pick it up and concatenate it into itself.

## Notes

`combined.txt` holds the whole of `splunk-cluster-aws-s3/` — 41 files, each
introduced by `==== FILE: <path> ====`.

Nothing may precede the first marker. The extractor prints every non-marker line
to the current filename, so a header or preamble is written to an empty filename
and awk fails with `cannot open "" for output`. That is why this lives in its own
file rather than at the top of the bundle.

Extraction is content-exact but not byte-exact: each file comes back with one
extra trailing blank line, from the separator the bundler writes between files.
Harmless for the YAML, JSON, `.conf` and Markdown here.

`combined.txt` is a derived copy sitting beside the directory it copies, so it
goes stale as soon as either side is edited. Regenerate rather than editing it.
