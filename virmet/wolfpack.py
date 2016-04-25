#!/usr/bin/env python3.4

'''Runs on all samples of a MiSeq run or on a single fastq file'''
import os
import sys
import glob
import logging
import pandas as pd
from virmet.common import run_child, single_process, DB_DIR

contaminant_db = ['/data/virmet_databases/human/bwa/humanGRCh38',
                  '/data/virmet_databases/bacteria/bwa/bact1',
                  '/data/virmet_databases/bacteria/bwa/bact2',
                  '/data/virmet_databases/bacteria/bwa/bact3',
                  '/data/virmet_databases/fungi/bwa/fungi1',
                  '/data/virmet_databases/bovine/bwa/bt_ref']
ref_map = {
    'humanGRCh38': '/data/virmet_databases/human/fasta/GRCh38.fasta.gz',
    'bact1': '/data/virmet_databases/bacteria/fasta/bact1.fasta.gz',
    'bact2': '/data/virmet_databases/bacteria/fasta/bact2.fasta.gz',
    'bact3': '/data/virmet_databases/bacteria/fasta/bact3.fasta.gz',
    'fungi1': '/data/virmet_databases/fungi/fasta/fungi1.fasta.gz',
    'bt_ref': '/data/virmet_databases/bovine/fasta/bt_ref_Bos_taurus_UMD_3.1.1.fasta.gz'
}

blast_cov_threshold = 75.
blast_ident_threshold = 75.


def hunter(fq_file):
    '''runs quality filter on a fastq file with seqtk and prinseq,
    simple parallelisation with xargs, returns output directory
    '''
    import re
    import warnings
    from virmet.common import prinseq_exe

    try:
        n_proc = min(os.cpu_count(), 16)
    except NotImplementedError:
        n_proc = 2

    logging.debug('hunter will run on %s processors' % n_proc)
    if '_' in fq_file:
        s_dir = '_'.join(os.path.split(fq_file)[1].split('_')[:2])
        try:
            os.mkdir(s_dir)
        except FileExistsError:
            logging.debug('entering %s already existing' % s_dir)
        os.chdir(s_dir)
        s_dir = os.getcwd()
    else:
        s_dir = os.getcwd()

    # first occurrence of stats.tsv
    oh = open('stats.tsv', 'w+')
    # count raw reads
    if fq_file.endswith('gz'):
        out1 = run_child('gunzip', '-c %s | wc -l' % fq_file)
    else:
        out1 = run_child('wc', '-l %s | cut -f 1 -d \" \"' % fq_file)
    n_reads = int(int(out1) / 4)
    oh.write('raw_reads\t%d\n' % n_reads)

    # trim and discard short reads, count
    logging.debug('trimming with seqtk')
    cml = 'trimfq %s | seqtk seq -L 75 - > intermediate.fastq' % fq_file
    out1 = run_child('seqtk', cml)
    out1 = run_child('wc', '-l intermediate.fastq | cut -f 1 -d \" \"')
    long_reads = int(int(out1) / 4)
    short = n_reads - long_reads
    oh.write('trimmed_too_short\t%d\n' % short)

    # We want to split in n_proc processors, so each file has at most
    # (n_reads / n_proc) + 1 reads and 4 times as many lines
    # this fails if there are more cpus than reads!
    max_reads_per_file = int(n_reads / n_proc) + 1
    max_l = max_reads_per_file * 4
    # split and rename
    run_child('split', '-l %d intermediate.fastq splitted' % max_l)
    os.remove('intermediate.fastq')
    splitted = glob.glob('splitted*')
    n_splitted = len(splitted)
    for i, spf in enumerate(sorted(splitted)):
        os.rename(spf, 'splitted%0.2d.fastq' % i)  # W.O. max 100 files/cpus

    # filter with prinseq, parallelize with xargs
    logging.debug('filtering with prinseq')
    cml = '-w 0 %d | xargs -P %d -I {} %s \
            -fastq splitted{}.fastq -lc_method entropy -lc_threshold 70 \
            -log prinseq{}.log -min_qual_mean 20 \
            -out_good ./good{} -out_bad ./bad{} > ./prinseq.err 2>&1' % (n_splitted - 1, n_splitted, prinseq_exe)
    run_child('seq', cml)

    logging.debug('cleaning up')
    if len(glob.glob('good??.fastq')):
        run_child('cat', 'good??.fastq > good.fastq')
        run_child('rm', 'good??.fastq')

    if len(glob.glob('bad??.fastq')):
        run_child('cat', 'bad??.fastq > bad.fastq')
        run_child('rm', 'bad??.fastq')

    if len(glob.glob('prinseq??.log')):
        run_child('cat', 'prinseq??.log > prinseq.log')
        run_child('rm', 'prinseq??.log')
    run_child('rm', 'splitted*fastq')

    # parsing number of reads deleted because of low entropy
    low_ent = 0
    min_qual = 0
    for l in open('prinseq.log'):
        match_lc = re.search('lc_method\:\s(\d*)$', l)
        match_mq = re.search('min_qual_mean\:\s(\d*)$', l)
        if match_lc:
            low_ent += int(match_lc.group(1))
        elif match_mq:
            min_qual += int(match_mq.group(1))
    oh.write('low_entropy\t%d\n' % low_ent)
    oh.write('low_quality\t%d\n' % min_qual)

    out1 = run_child('wc', '-l good.fastq | cut -f 1 -d \" \"')
    n_reads = int(int(out1) / 4)
    lost_reads = n_reads + low_ent + min_qual - long_reads
    if lost_reads > 0:
        logging.error('%d reads were lost' % lost_reads)
        warnings.warn('%d reads were lost' % lost_reads, RuntimeWarning)
    oh.write('passing_filter\t%d\n' % n_reads)

    os.chdir(os.pardir)
    return s_dir


def victor(input_reads, contaminant):
    '''decontaminate reads by aligning against contaminants with bwa and removing
    reads with alignments
    '''
    import gzip
    from Bio.SeqIO.QualityIO import FastqGeneralIterator
    try:
        n_proc = min(os.cpu_count(), 16)
    except NotImplementedError:
        n_proc = 2

    # alignment with bwa
    rf_head = input_reads.split('.')[0]
    cont_name = os.path.split(contaminant)[1]
    sam_name = '%s_%s.sam' % (rf_head, cont_name)
    err_name = '%s_%s.err' % (rf_head, cont_name)
    cml = 'mem -t %d -R \'@RG\tID:foo\tSM:bar\tLB:library1\' -T 75 -M %s %s 2> \
    %s | samtools view -h -F 4 - > %s' % (n_proc, contaminant, input_reads, err_name, sam_name)
    run_child('bwa', cml)
    logging.debug('running bwa %s %s on %d cores' % (cont_name, rf_head, n_proc))

    # reading sam file to remove reads with hits
    # test if an object is in set is way faster than in list
    mapped_reads = set(run_child('grep', '-v \"^@\" %s | cut -f 1' % sam_name).strip().split('\n'))
    try:  # if no matches, empty string is present
        mapped_reads.remove('')
    except KeyError:
        pass

    oh = open('stats.tsv', 'a')
    oh.write('matching_%s\t%d\n' % (cont_name, len(mapped_reads)))
    oh.close()
    clean_name = os.path.splitext(sam_name)[0] + '.fastq'

    output_handle = open(clean_name, 'w')
    logging.debug('Cleaning reads in %s with alignments in %s' %
                 (input_reads, sam_name))
    logging.debug('Writing to %s' % clean_name)
    if input_reads.endswith('.gz'):
        cont_handle = gzip.open(input_reads)
    else:
        cont_handle = open(input_reads)
    c = 0
    # Using FastqGeneralIterator allows fast performance
    for title, seq, qual in FastqGeneralIterator(cont_handle):
        if title.split()[0] not in mapped_reads:
            c += 1
            output_handle.write("@%s\n%s\n+\n%s\n" % (title, seq, qual))
            if c % 100000 == 0:
                logging.debug('written %d clean reads' % c)
    logging.info('written %d clean reads' % c)
    output_handle.close()

    return clean_name


def viral_blast(file_in, n_proc):
    '''runs blast against viral database, parallelise with xargs
    '''

    oh = open('stats.tsv', 'a')
    os.rename(file_in, 'hq_decont_reads.fastq')
    fasta_file = 'hq_decont_reads.fasta'
    run_child('seqtk', 'seq -A hq_decont_reads.fastq > %s' % fasta_file)
    tot_seqs = int(run_child('grep', '-c \"^>\" %s' % fasta_file).strip())
    oh.write('reads_to_blast\t%d\n' % tot_seqs)
    max_n = (tot_seqs / n_proc) + 1

    # We want to split in n_proc processors, so each file has at most
    # (tot_seqs / n_proc) + 1 reads
    cml = "-v \"MAX_N=%d\" \'BEGIN {n_seq=0;} /^>/ \
    {if(n_seq %% %d == 0){file=sprintf(\"splitted_clean_%%d.fasta\", n_seq/%d);} \
    print >> file; n_seq++; next;} { print >> file; }' %s" % (max_n, max_n, max_n, fasta_file)
    run_child('awk', cml)

    # blast needs access to taxdb files to retrieve organism name
    os.environ['BLASTDB'] = DB_DIR

    xargs_thread = 0  # means on all available cores, caution
    cml = '0 %s | xargs -P %d -I {} blastn -task megablast \
           -query splitted_clean_{}.fasta -db %s \
           -out tmp_{}.tsv \
           -outfmt \'6 qseqid sseqid sscinames stitle pident qcovs score length mismatch gapopen qstart qend sstart send staxids\'' \
        % (n_proc - 1, xargs_thread, os.path.join(DB_DIR, 'viral_nuccore/viral_db'))
    logging.debug('running blast now')
    run_child('seq', cml)

    logging.debug('parsing best HSP for each query sequence')
    qseqid = ''

    bh = open('unique.tsv', 'w')
    bh.write('qseqid\tsseqid\tsscinames\tstitle\tpident\tqcovs\tscore\tlength\tmismatch\tgapopen\tqstart\tqend\tsstart\tsend\tstaxids\n')
    for i in range(n_proc):
        tmpf = 'tmp_%d.tsv' % i
        with open(tmpf) as f:
            for line in f:
                if line.split('\t')[0] != qseqid:
                    bh.write(line)
                    qseqid = line.split('\t')[0]
        os.remove(tmpf)
        os.remove('splitted_clean_%d.fasta' % i)
    bh.close()

    logging.debug('filtering and grouping by scientific name')
    hits = pd.read_csv('unique.tsv', index_col='qseqid',  # delim_whitespace=True)
                       delimiter="\t")
    logging.debug('found %d hits' % hits.shape[0])

    # select according to identity and coverage, count occurrences
    good_hits = hits[(hits.pident > blast_ident_threshold) & \
        (hits.qcovs > blast_cov_threshold)]
    ds = good_hits.groupby('sscinames').size().order(ascending=False)
    org_count = pd.DataFrame({'organism': ds.index.tolist(), 'reads': ds.values},
                             index=ds.index)
    org_count.to_csv('orgs_list.tsv', header=True, sep='\t', index=False)
    matched_reads = good_hits.shape[0]
    logging.debug('%d hits passing coverage and identity filter' % matched_reads)
    oh.write('viral_reads\t%s\n' % matched_reads)
    unknown_reads = tot_seqs - matched_reads
    oh.write('undetermined_reads\t%d\n' % unknown_reads)
    oh.close()


def cleaning_up():
    '''sift reads into viral/unknown, compresses and removes files
    '''
    import multiprocessing as mp
    from Bio.SeqIO.QualityIO import FastqGeneralIterator

    # selects reads with coverage and identity higher than 75
    df = pd.read_csv('unique.tsv', sep='\t')
    viral_ids = set(df[(df.qcovs > blast_cov_threshold) & (df.pident > blast_ident_threshold)].qseqid)
    viral_c = 0
    undet_c = 0
    all_reads = 'hq_decont_reads.fastq'
    all_handle = open(all_reads)
    undet_handle = open('undetermined_reads.fastq', 'w')
    viral_handle = open('viral_reads.fastq', 'w')
    # Using FastqGeneralIterator allows fast performance
    for title, seq, qual in FastqGeneralIterator(all_handle):
        if title.split()[0] not in viral_ids:
            undet_c += 1
            undet_handle.write("@%s\n%s\n+\n%s\n" % (title, seq, qual))
            if undet_c % 100000 == 0:
                logging.debug('written %d undet reads' % undet_c)
        else:
            viral_c += 1
            viral_handle.write("@%s\n%s\n+\n%s\n" % (title, seq, qual))
            if viral_c % 10000 == 0:
                logging.debug('written %d viral reads' % viral_c)
    undet_handle.close()
    viral_handle.close()
    logging.info('written %d undet reads' % undet_c)
    logging.info('written %d viral reads' % viral_c)

    run_child('gzip', '-f viral_reads.fastq')
    run_child('gzip', '-f undetermined_reads.fastq')
    os.remove(all_reads)

    cmls = []
    for samfile in glob.glob('*.sam'):
        stem = os.path.splitext(samfile)[0]
        cont = stem.split('_')[-1]
        if cont == 'ref':  # hack because _ in bovine file name
            cont = 'bt_ref'
        cml = 'sort -O bam -l 0 -T /tmp -@ 4 %s | \
        samtools view -T %s -C -o %s.cram -@ 4 -' % (samfile, ref_map[cont], stem)
        cmls.append(('samtools', cml))

    # run in parallel
    pool = mp.Pool()
    results = pool.map(single_process, cmls)
    for r in results:
        logging.debug(r)

    # removing and zipping
    for samfile in glob.glob('*.sam'):
        os.remove(samfile)
    os.remove('good.fastq')
    os.remove('bad.fastq')
    os.remove('hq_decont_reads.fasta')
    run_child('rm', 'good_*fastq')
    run_child('gzip', '-f unique.tsv')


def main(args):
    ''''''

    if args.run:
        miseq_dir = args.run.rstrip('/')
        run_name = os.path.split(miseq_dir)[1]
        if run_name.startswith('1') and len(run_name.split('-')[-1]) == 5 and \
            run_name.split('_')[1].startswith('M'):
            try:
                run_date, machine_name = run_name.split('_')[:2]
                logging.info('running on run %s from machine %s' % (run_name, machine_name))
            except ValueError:
                logging.info('running on directory %s' % miseq_dir)
            bc_dir = os.path.join(miseq_dir, 'Data/Intensities/BaseCalls/')
        else:
            bc_dir = miseq_dir

        rel_fastq_files = glob.glob('%s/*_S*.fastq*' % bc_dir)
        samples_to_run = [os.path.split(fq)[1].split('_')[1] for fq in rel_fastq_files]
        logging.info('samples to run: %s' % ' '.join(samples_to_run))
        all_fastq_files = [os.path.abspath(f) for f in rel_fastq_files]
    elif args.file:
        all_fastq_files = [os.path.abspath(args.file)]
        logging.info('running on a single file %s' % all_fastq_files[0])
        run_name = args.file.split('.')[0]

    out_dir = 'virmet_output_%s' % run_name

    try:
        os.mkdir(out_dir)
    except OSError:
        logging.error('directory %s exists' % out_dir)
    os.chdir(out_dir)

    # run hunter on all fastq files
    s_dirs = []
    for fq in all_fastq_files:
        logging.info('running hunter on %s' % fq)
        sd = hunter(fq)
        s_dirs.append(sd)

    # run mapping against contaminants to remove
    cont_reads = 'good.fastq'  # first run on good.fastq
    for cont in contaminant_db:
        logging.info('decontamination against %s' % cont)
        for sample_dir in s_dirs:
            logging.info('--- now for sample %s' % sample_dir)
            os.chdir(sample_dir)
            decont_reads = victor(input_reads=cont_reads, contaminant=cont)
            os.chdir(os.pardir)
        cont_reads = decont_reads  # decontaminated reads are input for next round (equal across samples)

    logging.info('blasting against viral database')
    file_to_blast = cont_reads  # last output of victor is input for blast
    try:
        n_proc = min(os.cpu_count(), 12)
    except NotImplementedError:
        n_proc = 2
    logging.info('%d cores that will be used' % n_proc)

    for sample_dir in s_dirs:
        os.chdir(sample_dir)
        logging.info('now sample %s' % sample_dir)
        viral_blast(file_to_blast, n_proc)
        logging.info('sample %s blasted' % sample_dir)
        os.chdir(os.pardir)

    logging.info('summarising and cleaning up')
    for sample_dir in s_dirs:
        os.chdir(sample_dir)
        logging.info('now in %s' % sample_dir)
        cleaning_up()
        os.chdir(os.pardir)

    os.chdir(os.pardir)
    return out_dir
