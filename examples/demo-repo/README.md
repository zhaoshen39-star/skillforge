# tinycalc

**A tiny CLI that turns CSV into JSON.** No dependencies, reads stdin or a file.

## Install

```bash
pip install tinycalc
```

## Usage

```bash
tinycalc data.csv --pretty
cat data.csv | tinycalc --strict
```

Options: `--pretty` indents output, `--strict` fails on ragged rows.

## Why

Most CSV→JSON tools pull in a parser and a CLI framework. This one is 200 lines,
because the job is small.

## License

MIT
