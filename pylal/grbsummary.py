from __future__ import division

import os
import re
import socket
import sys
import tempfile
import time
import urlparse
itertools = __import__("itertools")  # absolute import of system-wide itertools

import numpy

from lalapps import inspiral
from glue import lal
from glue import iterutils
from glue import pipeline
from glue import segments, segmentsUtils
from glue.ligolw import ligolw
from glue.ligolw import table
from glue.ligolw import lsctables
from glue.ligolw import utils
from glue.ligolw.utils import ligolw_add
from pylal import llwapp
from pylal import rate

##############################################################################
# Custom classes
##############################################################################

class GRBSummaryDAG(pipeline.CondorDAG):
  def __init__(self, config_file, log_path):
    self.basename = config_file.replace(".ini", "")
    logfile, logfilename = tempfile.mkstemp(prefix=self.basename, suffix=".dag.log", dir=log_path)
    os.close(logfile)
    pipeline.CondorDAG.__init__(self, logfilename)
    self.set_dag_file(self.basename)

##############################################################################
# Convenience functions
##############################################################################
sensitivity_dict = {"H1": 1, "L1": 2, "H2": 3, "V1": 4, "G1": 5}
def sensitivity_cmp(ifo1, ifo2):
    """
    Provide a comparison operator for IFOs such that they would get sorted
    from most sensitive to least sensitive.
    """
    return cmp(sensitivity_dict[ifo1], sensitivity_dict[ifo2])

##############################################################################
# Segment-manipulation functions
##############################################################################

def compute_masked_segments(analyzable_seglist, on_source_segment,
    veto_seglist=None, quantization_time=None):
    """
    Return veto segmentlists for on-source and off-source regions,
    respectively.  Optionally, use vetos from veto_seglist.  Optionally,
    quantize the off-source with quantization_time (seconds).
    """
    analyzable_seglist = segments.segmentlist(analyzable_seglist[:]).coalesce()
    if veto_seglist is None:
        veto_seglist = segments.segmentlist()
    off_source_segs = analyzable_seglist - segments.segmentlist([on_source_segment])

    ## on-source mask
    on_source_mask = off_source_segs | veto_seglist

    ## off-source mask
    # first, assign its value without quantization
    off_source_mask = segments.segmentlist([on_source_segment]) | veto_seglist

    # then, quantize as necessary
    if quantization_time is not None:
        off_source_quantized = segments.segmentlist(
            [segments.segment(s[0], s[0] + abs(s)//quantization_time) \
             for s in (off_source_segs - off_source_mask)])
        off_source_mask = analyzable_seglist - off_source_quantized

    return on_source_mask, off_source_mask

def compute_offsource_segment(analyzable, on_source, padding_time=0,
    min_trials=None, max_trials=None, symmetric=True):
    """
    Compute and return the maximal off-source segment subject to the
    following constraints:
    
    1) The off-source segment is constrained to lie within a segment from the
       analyzable segment list and to contain the on_source segment.  If
       no such segment exists, return None.
    2) The off-source segment length is a multiple of the on-source segment
       length.  This multiple (minus one for the on-source segment) is called
       the number of trials.  By default, the number of trials is bounded
       only by the availability of analyzable time.

    Optionally:
    3) padding_time is subtracted from the analyzable segments, but added
       back to the off-source segment.  This represents time that is thrown
       away as part of the filtering process.
    4) max_trials caps the number of trials that the off-source segment
       can contain.  The truncation is performed so that the resulting
       off-source segment is as symmetric as possible.
    5) symmetric being True will simply truncate the off-source segment to
       be the symmetric about the on-source segment.
    """
    quantization_time = abs(on_source)
    
    try:
        super_seg = analyzable[analyzable.find(on_source)].contract(padding_time)
    except ValueError:
        return None
    
    # check again after taking padding into account
    if on_source not in super_seg:
        return None
    
    nplus = (super_seg[1] - on_source[1]) // quantization_time
    nminus = (on_source[0] - super_seg[0]) // quantization_time

    if (max_trials is not None) and (nplus + nminus > max_trials):
        half_max = max_trials // 2
        if nplus < half_max:
            # left sticks out, so cut it
            remainder = max_trials - nplus
            nminus = min(remainder, nminus)
        elif nminus < half_max:
            # right sticks out, so cut it
            remainder = max_trials - nminus
            nplus = min(remainder, nplus)
        else:
            # both sides stick out, so cut symmetrically
            nplus = nminus = half_max

    if symmetric:
        nplus = nminus = min(nplus, nminus)
    
    if (min_trials is not None) and (nplus + nminus < min_trials):
        return None

    return segments.segment((on_source[0] - nminus*quantization_time - padding_time,
                             on_source[1] + nplus*quantization_time + padding_time))

def multi_ifo_compute_offsource_segment(analyzable_dict, on_source, **kwargs):
    """
    Return the off-source segment determined for multiple IFO times along with
    the IFO combo that determined that segment.  Calls
    compute_offsource_segment as necessary, passing all kwargs as necessary.
    """
    # sieve down to relevant segments and IFOs; sort IFOs by sensitivity
    new_analyzable_dict = segments.segmentlistdict()
    for ifo, seglist in analyzable_dict.iteritems():
        try:
            ind = seglist.find(on_source)
        except ValueError:
            continue
        new_analyzable_dict[ifo] = segments.segmentlist([seglist[ind]])
    analyzable_ifos = new_analyzable_dict.keys()
    analyzable_ifos.sort(sensitivity_cmp)

    # now try getting off-source segments; start trying with all IFOs, then
    # work our way to smaller and smaller subsets; exclude single IFOs.    
    test_combos = itertools.chain( \
      *itertools.imap(lambda n: iterutils.choices(analyzable_ifos, n),
                      xrange(len(analyzable_ifos), 1, -1)))

    off_source_segment = None
    the_ifo_combo = []
    for ifo_combo in test_combos:
      trial_seglist = new_analyzable_dict.intersection(ifo_combo)
      temp_segment = compute_offsource_segment(trial_seglist, on_source,
                                               **kwargs)
      if temp_segment is not None:
        off_source_segment = temp_segment
        the_ifo_combo = list(ifo_combo)
        the_ifo_combo.sort()
        break
    
    return off_source_segment, the_ifo_combo

def get_segs_from_doc(doc):
    """
    Return the segments from a document
    @param doc: document containing the desired segments
    """

    # get segments
    seg_dict = llwapp.segmentlistdict_fromsearchsummary(doc)
    segs = seg_dict.union(seg_dict.iterkeys()).coalesce()
    # typecast to ints, which are better behaved and faster than LIGOTimeGPS
    segs = segments.segmentlist([segments.segment(int(seg[0]), int(seg[1]))\
        for seg in segs])

    return segs
   

def get_exttrig_trials_from_docs(onsource_doc, offsource_doc, veto_files):
    """
    Return a tuple of (off-source time bins, off-source veto mask,
    index of trial that is on source).
    The off-source veto mask is a one-dimensional boolean array where True
    means vetoed.
    @param onsource_doc: Document describing the on-source files
    @param offsource_doc: Document describing the off-source files
    @param veto_files: List of filenames containing vetoes 
    """

    # extract the segments
    on_segs = get_segs_from_docs(onsource_doc)
    off_segs = get_segs_from_docs(offsource_doc)

    return get_exttrig_trials(on_segs, off_segs, veto_files)

def get_exttrig_trials(on_segs, off_segs, veto_files):
    """
    Return a tuple of (off-source time bins, off-source veto mask,
    index of trial that is on source).
    The off-source veto mask is a one-dimensional boolean array where True
    means vetoed.
    @param on_segs: On-source segments
    @param off_segs: Off-source segments 
    @param veto_files: List of filenames containing vetoes
    """
   
    # Check that offsource length is a multiple of the onsource segment length
    trial_len = int(abs(on_segs))
    if abs(off_segs) % trial_len != 0:
        raise ValueError, "The provided file's analysis segment is not "\
            "divisible by the fold time."
    extent = off_segs.extent()

    # generate bins for trials
    num_trials = int(abs(extent)) // trial_len
    trial_bins = rate.LinearBins(extent[0], extent[1], num_trials)

    # incorporate veto file; in trial_veto_mask, True means vetoed.
    trial_veto_mask = numpy.zeros(num_trials, dtype=numpy.bool8)
    for veto_file in veto_files:
        new_veto_segs = segmentsUtils.fromsegwizard(open(veto_file),
                                                    coltype=int)
        if new_veto_segs.intersects(on_segs):
            print >>sys.stderr, "warning: %s overlaps on-source segment" \
                % veto_file
        trial_veto_mask |= rate.bins_spanned(trial_bins, new_veto_segs,
                                             dtype=numpy.bool8)

    # identify onsource trial index
    onsource_mask = rate.bins_spanned(trial_bins, on_segs, dtype=numpy.bool8)
    if sum(onsource_mask) != 1:
        raise ValueError, "on-source segment spans more or less than one trial"
    onsource_ind = numpy.arange(len(onsource_mask))[onsource_mask]

    return trial_bins, trial_veto_mask, onsource_ind

def get_mean_mchirp(coinc):
    """
    Return the arithmetic average of the mchirps of all triggers in coinc.
    """
    return sum(t.mchirp for t in coinc) / coinc.numifos

##############################################################################
# XML convenience code
##############################################################################

def load_external_triggers(filename):
    doc = ligolw_add.ligolw_add(ligolw.Document(), [filename])
    ext_trigger_tables = lsctables.getTablesByType(doc, lsctables.ExtTriggersTable)
    if ext_trigger_tables is None:
        print >>sys.stderr, "No tables named external_trigger:table found in " + filename
    else:
        assert len(ext_trigger_tables) == 1  # ligolw_add should merge them
        ext_triggers = ext_trigger_tables[0]
    return ext_triggers

def write_rows(rows, table_type, filename):
    """
    Create an empty LIGO_LW XML document, add a table of table_type,
    insert the given rows, then write the document to a file.
    """
    # prepare a new XML document
    xmldoc = ligolw.Document()
    xmldoc.appendChild(ligolw.LIGO_LW())
    tbl = lsctables.New(table_type)
    xmldoc.childNodes[-1].appendChild(tbl)
    
    # insert our rows
    tbl.extend(rows)
    
    # write out the document
    utils.write_filename(xmldoc, filename)

def load_cache(xmldoc, cache, sieve_pattern, exact_match=False,
    verbose=False):
    """
    Return a parsed and ligolw_added XML document from the files matched by
    sieve_pattern in the given cache.
    """
    subcache = cache.sieve(description=sieve_pattern, exact_match=exact_match)
    found, missed = subcache.checkfilesexist()
    if len(found) == 0:
        print >>sys.stderr, "warning: no files found for pattern %s" \
            % sieve_pattern

    # turn on event_id mangling
    old_id = lsctables.SnglInspiralTable.next_id
    lsctables.SnglInspiralTable.next_id = lsctables.SnglInspiralID_old(0)

    # reduce memory footprint at the expense of speed
    # table.RowBuilder = table.InterningRowBuilder

    urls = [c.url for c in found]
    try:
        xmldoc = ligolw_add.ligolw_add(ligolw.Document(), urls, verbose=verbose)
    except ligolw.ElementError:
        # FIXME: backwards compatibility for int_8s SnglInspiralTable event_ids
        lsctables.SnglInspiralTable.validcolumns["event_id"] = "int_8s"
        lsctables.SnglInspiralID = int
        xmldoc = ligolw_add.ligolw_add(ligolw.Document(), urls, verbose=verbose)

    # turn off event_id mangling
    lsctables.SnglInspiralTable.next_id = old_id

    return xmldoc

def get_num_slides(xmldoc):
    """
    Return the value of --num-slides found in the process_params table of
    xmldoc.  If no such entry is found, return 0.
    """
    tbl_name = lsctables.ProcessParamsTable.tableName

    # don't be too picky what program had --num-slides
    for tbl in table.getTablesByName(xmldoc, tbl_name):
        for row in tbl:
           if row.param == "--num-slides":
               return int(row.value)
    return 0

