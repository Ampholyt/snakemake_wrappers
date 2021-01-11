__author__ = "Max Schubach"
__copyright__ = "Copyright 2020, Max Schubach"
__email__ = "Max Schubach"
__license__ = "MIT"


import sys
import numpy as np
import json
import csv
import gzip
import math

import copy

import tensorflow as tf

from seqiolib import Sequence, Interval, Variant, Encoder, VariantType
from seqiolib import utils


from pyfaidx import Fasta
import pybedtools


altMinusRef = True
fileType = utils.FileType.TSV
if len(snakemake.params) > 0:
    if hasattr(snakemake.params,"altMinusRef"):
        altMinusRef = snakemake.params['altMinusRef']
    if hasattr(snakemake.params,"fileType"):
        if snakemake.params['fileType'] == "TSV":
            fileType = utils.FileType.TSV
        elif snakemake.params['fileType'] == "VCF":
            fileType = utils.FileType.VCF
        else:
            sys.exit("Unknown file format identifier %s. Can only be VCF or TSV" % snakemake.params['fileType'])

strategy = tf.distribute.MirroredStrategy()

def loadAndPredict(sequences, model, variants=None):
    X=[]
    i = 0
    for sequence in sequences:
        if (variants is not None):
            sequence.replace(variants[i])
        X.append(Encoder.one_hot_encode_along_channel_axis(sequence.getSequence()))
        i += 1
    prediction = model.predict(np.array(X))
    return(prediction)

def extendIntervals(intervals,region_length):
    left=math.ceil((region_length-1)/2)
    right=math.floor((region_length-1)/2)
    return(list(map(pybedtoolsIntervalToInterval,intervals.slop(r=right,l=left,g=str(snakemake.input.genome)))))

def variantToPybedtoolsInterval(variant):
    return(pybedtools.Interval(variant.contig, variant.position-1, variant.position))

def pybedtoolsIntervalToInterval(interval_pybed):
    return(Interval(interval_pybed.chrom, interval_pybed.start+1, interval_pybed.stop))

# load variants
variant_inputs = snakemake.input.variants if isinstance(snakemake.input.variants, list) else [snakemake.input.variants]

variants = []
for variant_input in variant_inputs:
    variants += utils.VariantIO.loadVariants(variant_input, fileType=fileType)
if len(variants) == 0:
    with gzip.open(snakemake.output.prediction, 'wt') as score_file:
        names=["#Chr","Pos","Ref","Alt"]
        score_writer = csv.DictWriter(score_file, fieldnames=names, delimiter='\t')
        score_writer.writeheader()
    exit()
# convert to intervals (pybedtools)
intervals = pybedtools.BedTool(list(map(variantToPybedtoolsInterval,variants)))

## maybe input is list or string. convert to list
model_file = snakemake.input.model if isinstance(snakemake.input.model, list) else [snakemake.input.model ]
weights_file = snakemake.input.weights if isinstance(snakemake.input.weights, list) else [snakemake.input.weights]

with strategy.scope():
    model = utils.io.ModelIO.loadModel(model_file[0], weights_file[0])

    input_length = model.input_shape[1]
    intervals = extendIntervals(intervals, input_length)


    # load sequence for variants
    reference = Fasta(snakemake.input.reference)
    sequences_ref = []
    sequences_alt = []

    for i in range(len(variants)):
        variant = variants[i]
        interval = intervals[i]

        # can be problematic if we are on the edges of a chromose.
        # Workaround. It is possible to extend the intreval left or right to get the correct length
        if (interval.length != input_length):
            print("Cannot use variant %s because of wrong size of interval %s " % (str(variant), str(interval)))
            continue

        sequence_ref = utils.io.SequenceIO.readSequence(reference,interval)

        # INDEL
        if (variant.type == VariantType.DELETION or variant.type == VariantType.INSERTION):
            # DELETION
            if (variant.type == VariantType.DELETION):
                extend = len(variant.ref) - len(variant.alt)
                if interval.isReverse():
                    interval.position = interval.position + extend
                else:
                    interval.position = interval.position - extend
                interval.length = interval.length + extend
            # INSERTION
            elif (variant.type == VariantType.INSERTION):
                extend = len(variant.alt) - len(variant.ref)
                if interval.isReverse():
                    interval.position = interval.position - extend
                else:
                    interval.position = interval.position + extend
                interval.length = interval.length - extend
            if (interval.length > 0):
                sequence_alt = utils.io.SequenceIO.readSequence(reference,interval)
                sequence_alt.replace(variant)
                if (len(sequence_alt.sequence) == input_length):
                    # FIXME: This is a hack. it seems that for longer indels the replacement does not work
                    sequences_alt.append(sequence_alt)
                    sequences_ref.append(sequence_ref)
                else:
                    print("Cannot use variant %s because of wrong interval %s has wrong size after InDel Correction" % (str(variant), str(interval)))
            else:
                print("Cannot use variant %s because interval %s has negative size" % (str(variant), str(interval)))
         # SNV
        else:
            sequence_alt = copy.copy(sequence_ref)
            sequence_alt.replace(variant)
            sequences_alt.append(sequence_alt)
            sequences_ref.append(sequence_ref)

    results_ref = loadAndPredict(sequences_ref,model)
    results_alt = loadAndPredict(sequences_alt,model)

with gzip.open(snakemake.output.prediction, 'wt') as score_file:
    names=["#Chr","Pos","Ref","Alt"]
    for task in range(results_ref.shape[1]):
        names += ["Task_%d_PredictionDelta" % task, "Task_%d_PredictionRef" % task,"Task_%d_PredictionAlt" % task]
    score_writer = csv.DictWriter(score_file, fieldnames=names, delimiter='\t')
    score_writer.writeheader()
    for i in range(results_ref.shape[0]):
        out =  {"#Chr": variants[i].contig, "Pos": variants[i].position, "Ref": variants[i].ref, "Alt":  variants[i].alt}
        for task in range(results_ref.shape[1]):
            out["Task_%d_PredictionDelta" % task] = results_alt[i][task]-results_ref[i][task] if altMinusRef else results_ref[i][task]-results_alt[i][task]
            out["Task_%d_PredictionRef" % task] = results_ref[i][task]
            out["Task_%d_PredictionAlt" % task] = results_alt[i][task]
        score_writer.writerow(out)
