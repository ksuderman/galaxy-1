"""
SLURM job control via the DRMAA API.
"""
import logging
import os
import subprocess
import time

from galaxy import model
from galaxy.jobs.runners.drmaa import DRMAAJobRunner

log = logging.getLogger( __name__ )

__all__ = ( 'SlurmJobRunner', )

SLURM_MEMORY_LIMIT_EXCEEDED_MSG = 'slurmstepd: error: Exceeded job memory limit'
SLURM_MEMORY_LIMIT_EXCEEDED_PARTIAL_WARNINGS = [': Exceeded job memory limit at some point.',
                                                ': Exceeded step memory limit at some point.']
SLURM_MEMORY_LIMIT_SCAN_SIZE = 16 * 1024 * 1024  # 16MB


class SlurmJobRunner( DRMAAJobRunner ):
    runner_name = "SlurmRunner"
    restrict_job_name_length = False

    def _complete_terminal_job( self, ajs, drmaa_state, **kwargs ):
        def _get_slurm_state_with_sacct(job_id, cluster):
            cmd = ['sacct', '-n', '-o state']
            if cluster:
                cmd.extend( [ '-M', cluster ] )
            cmd.extend(['-j', "%s.batch" % job_id])
            p = subprocess.Popen( cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE )
            stdout, stderr = p.communicate()
            if p.returncode != 0:
                stderr = stderr.strip()
                if stderr == 'SLURM accounting storage is disabled':
                    log.warning('SLURM accounting storage is not properly configured, unable to run sacct')
                    return
                raise Exception( '`%s` returned %s, stderr: %s' % ( ' '.join( cmd ), p.returncode, stderr ) )
            return stdout.strip()

        def _get_slurm_state():
            cmd = [ 'scontrol', '-o' ]
            if '.' in ajs.job_id:
                # custom slurm-drmaa-with-cluster-support job id syntax
                job_id, cluster = ajs.job_id.split('.', 1)
                cmd.extend( [ '-M', cluster ] )
            else:
                job_id = ajs.job_id
                cluster = None
            cmd.extend( [ 'show', 'job', job_id ] )
            p = subprocess.Popen( cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE )
            stdout, stderr = p.communicate()
            if p.returncode != 0:
                # Will need to be more clever here if this message is not consistent
                if stderr == 'slurm_load_jobs error: Invalid job id specified\n':
                    # The job may be old, try to get its state with sacct
                    job_state = _get_slurm_state_with_sacct(job_id, cluster)
                    if job_state:
                        return job_state
                    return 'NOT_FOUND'
                raise Exception( '`%s` returned %s, stderr: %s' % ( ' '.join( cmd ), p.returncode, stderr ) )
            job_info_dict = dict( [ out_param.split( '=', 1 ) for out_param in stdout.split() ] )
            return job_info_dict['JobState']

        try:
            if drmaa_state == self.drmaa_job_states.FAILED:
                slurm_state = _get_slurm_state()
                sleep = 1
                while slurm_state == 'COMPLETING':
                    log.debug( '(%s/%s) Waiting %s seconds for failed job to exit COMPLETING state for post-mortem', ajs.job_wrapper.get_id_tag(), ajs.job_id, sleep )
                    time.sleep( sleep )
                    sleep *= 2
                    if sleep > 64:
                        ajs.fail_message = "This job failed and the system timed out while trying to determine the cause of the failure."
                        break
                    slurm_state = _get_slurm_state()
                if slurm_state == 'NOT_FOUND':
                    log.warning( '(%s/%s) Job not found, assuming job check exceeded MinJobAge and completing as successful', ajs.job_wrapper.get_id_tag(), ajs.job_id )
                    drmaa_state = self.drmaa_job_states.DONE
                elif slurm_state == 'TIMEOUT':
                    log.info( '(%s/%s) Job hit walltime', ajs.job_wrapper.get_id_tag(), ajs.job_id )
                    ajs.fail_message = "This job was terminated because it ran longer than the maximum allowed job run time."
                    ajs.runner_state = ajs.runner_states.WALLTIME_REACHED
                elif slurm_state == 'NODE_FAIL':
                    log.warning( '(%s/%s) Job failed due to node failure, attempting resubmission', ajs.job_wrapper.get_id_tag(), ajs.job_id )
                    ajs.job_wrapper.change_state( model.Job.states.QUEUED, info='Job was resubmitted due to node failure' )
                    try:
                        self.queue_job( ajs.job_wrapper )
                        return
                    except:
                        ajs.fail_message = "This job failed due to a cluster node failure, and an attempt to resubmit the job failed."
                elif slurm_state == 'CANCELLED':
                    # Check to see if the job was killed for exceeding memory consumption
                    if self.__check_memory_limit( ajs.error_file ):
                        log.info( '(%s/%s) Job hit memory limit', ajs.job_wrapper.get_id_tag(), ajs.job_id )
                        ajs.fail_message = "This job was terminated because it used more memory than it was allocated."
                        ajs.runner_state = ajs.runner_states.MEMORY_LIMIT_REACHED
                    else:
                        log.info( '(%s/%s) Job was cancelled via slurm (e.g. with scancel(1))', ajs.job_wrapper.get_id_tag(), ajs.job_id )
                        ajs.fail_message = "This job failed because it was cancelled by an administrator."
                elif slurm_state in ('PENDING', 'RUNNING'):
                    log.warning( '(%s/%s) Job was reported by drmaa as terminal but job state in SLURM is: %s, returning to monitor queue', ajs.job_wrapper.get_id_tag(), ajs.job_id, slurm_state )
                    return True
                else:
                    log.warning( '(%s/%s) Job failed due to unknown reasons, job state in SLURM was: %s', ajs.job_wrapper.get_id_tag(), ajs.job_id, slurm_state )
                    ajs.fail_message = "This job failed for reasons that could not be determined."
                if drmaa_state == self.drmaa_job_states.FAILED:
                    ajs.fail_message += '\nPlease click the bug icon to report this problem if you need help.'
                    ajs.stop_job = False
                    self.work_queue.put( ( self.fail_job, ajs ) )
                    return
            if drmaa_state == self.drmaa_job_states.DONE:
                with open(ajs.error_file, 'r+') as f:
                    if os.path.getsize(ajs.error_file) > SLURM_MEMORY_LIMIT_SCAN_SIZE:
                        f.seek(-SLURM_MEMORY_LIMIT_SCAN_SIZE, os.SEEK_END)
                        f.readline()
                    pos = f.tell()
                    lines = f.readlines()
                    f.seek(pos)
                    for line in lines:
                        stripped_line = line.strip()
                        if any([_ in stripped_line for _ in SLURM_MEMORY_LIMIT_EXCEEDED_PARTIAL_WARNINGS]):
                            log.debug( '(%s/%s) Job completed, removing SLURM exceeded memory warning: "%s"', ajs.job_wrapper.get_id_tag(), ajs.job_id, stripped_line )
                        else:
                            f.write(line)
                    f.truncate()
        except Exception:
            log.exception( '(%s/%s) Failure in SLURM _complete_terminal_job(), job final state will be: %s', ajs.job_wrapper.get_id_tag(), ajs.job_id, drmaa_state )
        # by default, finish the job with the state from drmaa
        return super( SlurmJobRunner, self )._complete_terminal_job( ajs, drmaa_state=drmaa_state )

    def __check_memory_limit( self, efile_path ):
        """
        A very poor implementation of tail, but it doesn't need to be fancy
        since we are only searching the last 2K
        """
        try:
            log.debug( 'Checking %s for exceeded memory message from slurm', efile_path )
            with open( efile_path ) as f:
                if os.path.getsize(efile_path) > 2048:
                    f.seek(-2048, os.SEEK_END)
                    f.readline()
                for line in f.readlines():
                    if line.strip() == SLURM_MEMORY_LIMIT_EXCEEDED_MSG:
                        return True
        except:
            log.exception('Error reading end of %s:', efile_path)

        return False
