#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""subcmd_extract.py

Provides the extract subcommand for pdp.py

(c) The James Hutton Institute 2017-2019

Author: Leighton Pritchard
Contact: leighton.pritchard@hutton.ac.uk

Leighton Pritchard,
Information and Computing Sciences,
James Hutton Institute,
Errol Road,
Invergowrie,
Dundee,
DD2 5DA,
Scotland,
UK

The MIT License

Copyright (c) 2017-2019 The James Hutton Institute
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import multiprocessing
import os
import shutil

from Bio import AlignIO, SeqIO
from joblib import Parallel, delayed
from tqdm import tqdm

from diagnostic_primers import extract, load_primers
from diagnostic_primers.extract import PDPAmpliconError
from diagnostic_primers.scripts.tools import (
    collect_existing_output,
    create_output_directory,
    load_config_json,
    log_clines,
    run_parallel_jobs,
)


def extract_primers(task_name, primer, coll, outdir, limits):
    """Convenience function for parallelising primer extraction

    :param task_name:
    :param primer:
    :param coll:
    :param outdir:
    :param limits:        tuple - minimum and maximum amplicon lengths to consider

    Returns dict of primer identity and FASTA file path
    """
    amplicons, _ = extract.extract_amplicons(task_name, primer, coll, limits)

    amplicon_fasta = {}
    for pname in amplicons.primer_names:
        seqoutfname = os.path.join(outdir, pname + ".fasta")
        amplicons.write_amplicon_sequences(pname, seqoutfname)
        amplicon_fasta[pname] = seqoutfname

    return amplicon_fasta


def recover_existing_aln_files(args, logger, outdir):
    """Return list of existing alignment files if in recovery mode

    :param args:  Namespace of command-line arguments
    :param logger: logging object
    :param outdir:  Path to output directory

    Returns an empty list, unless in recovery mode. In recovery mode, a list
    of existing output files is returned
    """
    if args.recovery:
        logger.warning("Entering recovery mode")
        logger.info(
            "\tIn this mode, existing comparison output from %s is reused", outdir
        )
        existingfiles = collect_existing_output(outdir, "extract", args)
        logger.info(
            "Existing files found:\n\t%s", "\n\t".join([_ for _ in existingfiles])
        )
        return existingfiles
    return []


def mafft_align_sequences(args, logger, amplicon_fasta, outdir):
    """Align amplicon sequences using MAFFT

    :param args: Namespace of command-line arguments
    :param logger: logging object
    :param amplicon_fasta: dictionary of amplicon sequences keyed by filename
    :param outdir: path to output directory
    """
    # If we are in recovery mode, we are salvaging output from a previous
    # run, and do not necessarily need to rerun all the jobs. In this case,
    # we prepare a list of output files we want to recover from the results
    # in the output directory.
    amplicon_alnfiles = {}
    existingfiles = recover_existing_aln_files(args, logger, outdir)
    clines = []
    logger.info(
        "Compiling MAFFT alignment commands for %d amplicons", len(amplicon_fasta)
    )
    for pname, fname in tqdm(
        amplicon_fasta.items(),
        desc="compiling MAFFT commands",
        disable=args.disable_tqdm,
    ):
        alnoutfname = os.path.join(outdir, pname + ".aln")
        amplicon_alnfiles[pname] = alnoutfname
        with open(fname, "r") as ifh:
            if len(list(SeqIO.parse(ifh, "fasta"))) < 2:
                logger.warning(
                    "Output amplicon file %s cannot be aligned with MAFFT (copying)",
                    fname,
                )
                shutil.copyfile(fname, alnoutfname)
        if os.path.split(alnoutfname)[-1] not in existingfiles:  # skip if file exists
            # MAFFT is run with --quiet flag to suppress verbiage in STDERR
            clines.append(
                "pdp_mafft_wrapper.py {} --quiet {} {}".format(
                    args.mafft_exe, fname, alnoutfname
                )
            )
    # Pass command-lines to the appropriate scheduler
    if clines:
        logger.info("Aligning amplicons with MAFFT")
        logger.info("MAFFT command lines:\n\t%s", "\n\t".join(clines))
        pretty_clines = [str(c).replace(" -", " \\\n          -") for c in clines]
        log_clines(pretty_clines, logger)
        run_parallel_jobs(clines, args, logger)
    else:
        logger.warning(
            "No MAFFT jobs were scheduled (you may see this if the --recovery option is active)"
        )
    return amplicon_alnfiles


def subcmd_extract(args, logger, use_parallelism=True):
    """Extract amplicons corresponding to primer sets.

    :param args:   Namespace of command-line arguments
    :param logger: logging object
    :param use_parallelism:   boolean flag for debugging
        if set to True, use joblib to parallelise tasks; set to False to aid
        with debugging/localising issues
    """
    logger.info("Extracting amplicons for primer set %s", args.primerfile)
    logger.info("PrimerSearch and genome information provided by %s", args.infilename)
    if not args.noalign:
        logger.info("MAFFT executable for alignment: %s", args.mafft_exe)

    # Create output directory, if needed
    task_name = os.path.splitext(os.path.split(args.primerfile)[-1])[0]
    outdir = os.path.join(args.outdir, task_name)
    create_output_directory(outdir, args.ex_force, logger)

    # Load the config file and extract the amplicons for each primer set
    # in turn. Put the amplicons into a .fasta file and record the location
    # for each primer set
    logger.info("Loading primers from %s", args.primerfile)
    primers = load_primers(args.primerfile, fmt="json")
    coll = load_config_json(args, logger)

    # Run parallel extractions of primers
    logger.info("Extracting amplicons from source genomes")
    if use_parallelism:
        results = Parallel(n_jobs=multiprocessing.cpu_count())(
            delayed(extract_primers)(
                task_name,
                primer,
                coll,
                outdir,
                (args.ex_minamplicon, args.ex_maxamplicon),
            )
            for primer in tqdm(
                primers, desc="extracting amplicons", disable=args.disable_tqdm
            )
        )
    else:
        results = []
        for primer in tqdm(
            primers, desc="extracting amplicons", disable=args.disable_tqdm
        ):
            result = extract_primers(
                task_name,
                primer,
                coll,
                outdir,
                (args.ex_minamplicon, args.ex_maxamplicon),
            )
            results.append(result)
    amplicon_seqfiles = dict(pair for d in results for pair in d.items())

    # Align the sequences with MAFFT
    if not args.noalign:
        amplicon_seqfiles = mafft_align_sequences(
            args, logger, amplicon_seqfiles, outdir
        )

    # Calculate distance matrix information and write to file
    logger.info("Calculating distance matrices")
    distoutfname = os.path.join(outdir, "distances_summary.tab")
    logger.info("Writing distance metric summaries to %s", distoutfname)
    with open(distoutfname, "w") as ofh:
        ofh.write(
            "\t".join(
                [
                    "primer",
                    "dist_mean",
                    "dist_sd",
                    "dist_min",
                    "dist_max",
                    "unique",
                    "nonunique",
                    "shannon_index",
                    "shannon_evenness",
                ]
            )
            + "\n"
        )
        # Note: ordered output for the table; tqdm call returns
        # (primer filename, alignment filename)
        for fnames in tqdm(
            sorted(amplicon_seqfiles.items()),
            desc="processing alignments",
            disable=args.disable_tqdm,
        ):
            try:
                result = extract.calculate_distance(
                    AlignIO.read(open(fnames[1]), "fasta")
                )
            except PDPAmpliconError as exc:  # Catches alignment/calculation problems
                logger.warning("Distance calculation error: %s", exc)
                result = extract.DistanceResults(None, [], 0, 0, 0, 0, 0, 0, 0, 0)
            ofh.write(
                "\t".join(
                    [
                        fnames[0],
                        "%0.4f" % result.mean,
                        "%0.4f" % result.sd,
                        "%0.4f" % result.min,
                        "%0.4f" % result.max,
                        "%d" % result.unique,
                        "%d" % result.nonunique,
                        "%.02f" % result.shannon,
                        "%.02f" % result.evenness,
                    ]
                )
                + "\n"
            )

    return 0
