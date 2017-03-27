
__copyright__ = "Copyright 2013-2016, http://radical.rutgers.edu"
__license__   = "MIT"


import os
import copy
import math
import time
import pprint
import shutil
import tempfile
import threading

import subprocess           as sp
import threading            as mt

import saga                 as rs
import saga.utils.pty_shell as rsup
import radical.utils        as ru

from .... import pilot      as rp
from ...  import utils      as rpu
from ...  import states     as rps
from ...  import constants  as rpc

from .base import PMGRLaunchingComponent


# ------------------------------------------------------------------------------
# local constants
DEFAULT_AGENT_SPAWNER = 'POPEN'
DEFAULT_RP_VERSION    = 'local'
DEFAULT_VIRTENV       = '%(global_sandbox)s/ve'
DEFAULT_VIRTENV_MODE  = 'update'
DEFAULT_AGENT_CONFIG  = 'default'

JOB_CANCEL_DELAY      = 12000# seconds between cancel signal and job kill
JOB_CHECK_INTERVAL    =  60  # seconds between runs of the job state check loop
JOB_CHECK_MAX_MISSES  =   3  # number of times to find a job missing before
                             # declaring it dead

LOCAL_SCHEME = 'file'
BOOTSTRAPPER = "bootstrap_1.sh"

# ==============================================================================
#
class Default(PMGRLaunchingComponent):

    # --------------------------------------------------------------------------
    #
    def __init__(self, cfg, session):

        PMGRLaunchingComponent.__init__(self, cfg, session)


    # --------------------------------------------------------------------------
    #
    def initialize_child(self):

        self.register_input(rps.PMGR_LAUNCHING_PENDING, 
                            rpc.PMGR_LAUNCHING_QUEUE, self.work)

        # we don't really have an output queue, as we pass control over the
        # pilot jobs to the resource management system (RM).

        self._pilots        = dict()             # dict for all known pilots
        self._pilots_lock   = threading.RLock()  # lock on maipulating the above
        self._checking      = list()             # pilots to check state on
        self._check_lock    = threading.RLock()  # lock on maipulating the above
        self._saga_fs_cache = dict()             # cache of saga directories
        self._saga_js_cache = dict()             # cache of saga job services
        self._sandboxes     = dict()             # cache of global sandbox URLs
        self._cache_lock    = threading.RLock()  # lock for cache

        self._mod_dir       = os.path.dirname(os.path.abspath(__file__))
        self._root_dir      = "%s/../../"   % self._mod_dir  
        self._conf_dir      = "%s/configs/" % self._root_dir 

        # FIXME: make interval configurable
        self.register_timed_cb(self._pilot_watcher_cb, timer=10.0)
        
        # we listen for pilot cancel commands
        self.register_subscriber(rpc.CONTROL_PUBSUB, self._pmgr_control_cb)

        _, _, _, self._rp_sdist_name, self._rp_sdist_path = \
                ru.get_version([self._root_dir, self._mod_dir])


    # --------------------------------------------------------------------------
    #
    def finalize_child(self):

        self._log.debug('finalize child')
        # avoid shutdown races:
        
        self.unregister_timed_cb(self._pilot_watcher_cb)
        self.unregister_subscriber(rpc.CONTROL_PUBSUB, self._pmgr_control_cb)

        with self._cache_lock:
            for url,js in self._saga_js_cache.iteritems():
                self._log.debug('close  js to %s', url)
                js.close()
                self._log.debug('closed js to %s', url)
            self._saga_js_cache.clear()
        self._log.debug('finalized child')


    # --------------------------------------------------------------------------
    #
    def _pmgr_control_cb(self, topic, msg):

        cmd = msg['cmd']
        arg = msg['arg']

        self._log.debug('launcher got %s', msg)

        if cmd == 'cancel_pilots':

            # on cancel_pilot requests, we forward the DB entries via MongoDB,
            # by pushing a pilot update.  We also mark the pilot for
            # cancelation, so that the pilot watcher can cancel the job after
            # JOB_CANCEL_DELAY seconds, in case the pilot did not react on the
            # command in time.

            pmgr = arg['pmgr']
            pids = arg['uids']

            if pmgr != self._pmgr:
                # this request is not for us to enact
                return

            if not isinstance(pids, list):
                pids = [pids]

            self._log.info('received pilot_cancel command (%s)', pids)

            # send the cancelation equest to the pilots
            self._session._dbs.pilot_command('cancel_pilot', [], pids)

            # recod time of request, so that forceful termination can happen
            # after a certain delay
            now = time.time()
            with self._pilots_lock:
                for pid in pids:
                    self._pilots[pid]['pilot']['cancel_requested'] = now


    # --------------------------------------------------------------------------
    #
    def _pilot_watcher_cb(self):

        # FIXME: we should actually use SAGA job state notifications!
        # FIXME: check how race conditions are handles: we may detect
        #        a finalized SAGA job and change the pilot state -- but that
        #        pilot may have transitioned into final state via the normal
        #        notification mechanism already.  That probably should be sorted
        #        out by the pilot manager, which will receive notifications for
        #        both transitions.  As long as the final state is the same,
        #        there should be no problem anyway.  If it differs, the
        #        'cleaner' final state should prevail, in this ordering:
        #          cancel
        #          timeout
        #          error
        #          disappeared
        #        This implies that we want to communicate 'final_cause'

        # we don't want to lock our members all the time.  For that reason we
        # use a copy of the pilots_tocheck list and iterate over that, and only
        # lock other members when they are manipulated.

        ru.raise_on('pilot_watcher_cb')
        
        tc = rs.job.Container()
        with self._pilots_lock, self._check_lock:

            for pid in self._checking:
                tc.add(self._pilots[pid]['job'])

        states = tc.get_states()

        # if none of the states is final, we have nothing to do.
        # We can't rely on the ordering of tasks and states in the task
        # container, so we hope that the task container's bulk state query lead
        # to a caching of state information, and we thus have cache hits when
        # querying the pilots individually

        final_pilots = list()
        with self._pilots_lock, self._check_lock:
            for pid in self._checking:
                state = self._pilots[pid]['job'].state
                if state in [rs.job.DONE, rs.job.FAILED, rs.job.CANCELED]:
                    pilot = self._pilots[pid]['pilot']
                    if state == rs.job.DONE    : pilot['state'] = rps.DONE
                    if state == rs.job.FAILED  : pilot['state'] = rps.FAILED
                    if state == rs.job.CANCELED: pilot['state'] = rps.CANCELED
                    final_pilots.append(pilot)

        if final_pilots:

            for pilot in final_pilots:

                with self._check_lock:
                    # stop monitoring this pilot
                    self._checking.remove(pilot['uid'])

            self.advance(final_pilots, push=False, publish=True)

        # all checks are done, final pilots are weeded out.  Now check if any
        # pilot is scheduled for cancellation and is overdue, and kill it
        # forcefully.
        to_cancel  = list()
        to_advance = list()
        with self._pilots_lock:

            for pid in self._pilots:

                pilot   = self._pilots[pid]['pilot']
                time_cr = pilot.get('cancel_requested')

                # check if the pilot is final meanwhile
                if pilot['state'] in rps.FINAL:
                    continue

                if time_cr and time_cr + JOB_CANCEL_DELAY < time.time():
                    del(pilot['cancel_requested'])
                    to_cancel.append(pid)

        if not to_cancel:
            return

        tc = rs.task.Container()
        with self._pilots_lock:

            for pid in to_cancel:

                if pid not in self._pilots:
                    self._log.error('unknown: %s', pid)
                    raise ValueError('unknown pilot %s' % pid)

                pilot = self._pilots[pid]['pilot']
                job   = self._pilots[pid]['job']
                to_advance.append(pilot)
                tc.add(job)

        tc.cancel()
        tc.wait()

        # we don't want the watcher checking for this pilot anymore
        with self._check_lock:
            for pid in to_cancel:
                if pid in self._checking:
                    self._checking.remove(pid)

        # set canceled state
        to_advance = list()
        with self._pilots_lock:

            for pid in to_cancel:

                pilot = self._pilots[pid]['pilot']
                to_advance.append(pilot)

        self.advance(to_advance, state=rps.CANCELED, push=False, publish=True)


    # --------------------------------------------------------------------------
    #
    def work(self, pilots):

        if not isinstance(pilots, list):
            pilots = [pilots]

        self.advance(pilots, rps.PMGR_LAUNCHING, publish=True, push=False)

        # We can only use bulk submission for pilots which go to the same
        # target, thus we sort them into buckets and lunch the buckets
        # individually
        buckets = dict()
        for pilot in pilots:
            resource = pilot['description']['resource']
            schema   = pilot['description']['access_schema']
            if resource not in buckets:
                buckets[resource] = dict()
            if schema not in buckets[resource]:
                buckets[resource][schema] = list()
            buckets[resource][schema].append(pilot)

        for resource in buckets:

            for schema in buckets[resource]:

                try:
                    pilots = buckets[resource][schema]
                    pids   = [p['uid'] for p in pilots]
                    self._log.info("Launching pilots on %s: %s", resource, pids)
                                   
                    self._start_pilot_bulk(resource, schema, pilots)
        
                    self.advance(pilots, rps.PMGR_ACTIVE_PENDING, push=False, publish=True)


                except Exception as e:
                    self._log.exception('bulk launch failed')
                    self.advance(pilots, rps.FAILED, push=False, publish=True)


    # --------------------------------------------------------------------------
    #
    def _start_pilot_bulk(self, resource, schema, pilots):
        """
        For each pilot, we prepare by determining what files need to be staged,
        and what job description needs to be submitted.

        We expect `_prepare_pilot(resource, pilot)` to return a dict with:

            { 
              'js' : saga.job.Description,
              'ft' : [ 
                { 'src' : string  # absolute source file name
                  'tgt' : string  # relative target file name
                  'rem' : bool    # shall we remove src?
                }, 
                ... ]
            }
        
        When transfering data, we'll ensure that each src is only transferred
        once (in fact, we put all src files into a tarball and unpack that on
        the target side).

        The returned dicts are expected to only contain files which actually
        need staging, ie. which have not been staged during a previous pilot
        submission.  That implies one of two things: either this component is
        stateful, and remembers what has been staged -- which makes it difficult
        to use multiple component instances; or the component inspects the
        target resource for existing files -- which involves additional
        expensive remote hops.
        FIXME: since neither is implemented at this point we won't discuss the
               tradeoffs further -- right now files are unique per pilot bulk.

        Once all dicts are collected, we create one additional file which
        contains the staging information, and then pack all src files into
        a tarball for staging.  We transfer the tarball, and *immediately*
        trigger the untaring on the target resource, which is thus *not* part of
        the bootstrapping process.
        NOTE: this is to avoid untaring race conditions for multiple pilots, and
              also to simplify bootstrapping dependencies -- the bootstrappers
              are likely within the tarball after all...
        """

        rcfg = self._session.get_resource_config(resource, schema)
        sid  = self._session.uid

        # we create a fake session_sandbox with all pilot_sandboxes in /tmp, and
        # then tar it up.  Once we untar that tarball on the target machine, we
        # should have all sandboxes and all files required to bootstrap the
        # pilots
        # FIXME: on untar, there is a race between multiple launcher components
        #        within the same session toward the same target resource.
        tmp_dir  = os.path.abspath(tempfile.mkdtemp(prefix='rp_agent_tar_dir'))
        tar_name = '%s.%s.tgz' % (sid, self.uid)
        tar_tgt  = '%s/%s'     % (tmp_dir, tar_name)
        tar_url  = rs.Url('file://localhost/%s' % tar_tgt)

        # we need the session sandbox url, but that is (at least in principle)
        # dependent on the schema to use for pilot startup.  So we confirm here
        # that the bulk is consistent wrt. to the schema.
        # FIXME: if it is not, it needs to be splitted into schema-specific
        # sub-bulks
        schema = pilots[0]['description'].get('access_schema')
        for pilot in pilots[1:]:
            assert(schema == pilot['description'].get('access_schema'))

        session_sandbox = self._session._get_session_sandbox(pilots[0]).path

        # we will create the session sandbox before we untar, so we can use that
        # as workdir, and pack all paths relative to that session sandbox.  That
        # implies that we have to recheck that all URLs in fact do point into
        # the session sandbox.

        ft_list = list()  # files to stage
        jd_list = list()  # jobs  to submit
        for pilot in pilots:
            info = self._prepare_pilot(resource, rcfg, pilot)
            ft_list += info['ft']
            jd_list.append(info['jd'])

        for ft in ft_list:
            src     = os.path.abspath(ft['src'])
            tgt     = os.path.relpath(os.path.normpath(ft['tgt']), session_sandbox)
            src_dir = os.path.dirname(src)
            tgt_dir = os.path.dirname(tgt)

            if tgt_dir.startswith('..'):
                raise ValueError('staging target %s outside of pilot sandbox' % ft['tgt'])

            if not os.path.isdir('%s/%s' % (tmp_dir, tgt_dir)):
                os.makedirs('%s/%s' % (tmp_dir, tgt_dir))

            if src == '/dev/null' :
                # we want an empty file -- touch it (tar will refuse to 
                # handle a symlink to /dev/null)
                open('%s/%s' % (tmp_dir, tgt), 'a').close()
            else:
                os.symlink(src, '%s/%s' % (tmp_dir, tgt))

        # tar.  If any command fails, this will raise.
        cmd = "cd %s && tar zchf %s *" % (tmp_dir, tar_tgt)
        self._log.debug('cmd: %s', cmd)
        out = sp.check_output(["/bin/sh", "-c", cmd], stderr=sp.STDOUT)
        self._log.debug('out: %s', out)

        # remove all files marked for removal-after-pack
        for ft in ft_list:
            if ft['rem']:
                os.unlink(ft['src'])

        fs_endpoint = rcfg['filesystem_endpoint']
        fs_url      = rs.Url(fs_endpoint)

        self._log.debug ("rs.file.Directory ('%s')" % fs_url)

        with self._cache_lock:
            if fs_url in self._saga_fs_cache:
                fs = self._saga_fs_cache[fs_url]
            else:
                fs = rs.filesystem.Directory(fs_url, session=self._session)
                self._saga_fs_cache[fs_url] = fs

        tar_rem      = rs.Url(fs_url)
        tar_rem.path = "%s/%s" % (session_sandbox, tar_name)

        fs.copy(tar_url, tar_rem, flags=rs.filesystem.CREATE_PARENTS)

        shutil.rmtree(tmp_dir)


        # we now need to untar on the target machine -- if needed use the hop
        js_ep   = rcfg['job_manager_endpoint']
        js_hop  = rcfg.get('job_manager_hop', js_ep)
        js_url  = rs.Url(js_hop)

        # well, we actually don't need to talk to the lrms, but only need
        # a shell on the headnode.  That seems true for all LRMSs we use right
        # now.  So, lets convert the URL:
        if '+' in js_url.scheme:
            parts = js_url.scheme.split('+')
            if 'gsissh' in parts: js_url.scheme = 'gsissh'
            elif  'ssh' in parts: js_url.scheme = 'ssh'
        else:
            # In the non-combined '+' case we need to distinguish between
            # a url that was the result of a hop or a local lrms.
            if js_url.scheme not in ['ssh', 'gsissh']:
                js_url.scheme = 'fork'

        with self._cache_lock:
            if  js_url in self._saga_js_cache:
                js_tmp  = self._saga_js_cache[js_url]
            else:
                js_tmp  = rs.job.Service(js_url, session=self._session)
                self._saga_js_cache[js_url] = js_tmp
     ## cmd = "tar zmxvf %s/%s -C / ; rm -f %s" % \
        cmd = "tar zmxvf %s/%s -C %s" % \
                (session_sandbox, tar_name, session_sandbox)
        j = js_tmp.run_job(cmd)
        j.wait()

        self._log.debug('tar cmd : %s', cmd)
        self._log.debug('tar done: %s, %s, %s', j.state, j.stdout, j.stderr)

        # look up or create JS for actual pilot submission.  This might result
        # in the same JS, or not.
        with self._cache_lock:
            if js_ep in self._saga_js_cache:
                js = self._saga_js_cache[js_ep]
            else:
                js = rs.job.Service(js_ep, session=self._session)
                self._saga_js_cache[js_ep] = js

        # now that the scripts are in place and configured, 
        # we can launch the agent
        jc = rs.job.Container()

        for jd in jd_list:
            self._log.debug('jd: %s', pprint.pformat(jd.as_dict()))
            jc.add(js.create_job(jd))

        jc.run()

        for j in jc.get_tasks():

            # do a quick error check
            if j.state == rs.FAILED:
                self._log.error('%s: %s : %s : %s', j.id, j.state, j.stderr, j.stdout)
                raise RuntimeError ("SAGA Job state is FAILED.")

            if not j.name:
                raise RuntimeError('cannot get job name for %s' % j.id)

            pilot = None
            for p in pilots:
                if p['uid'] == j.name:
                    pilot = p
                    break

            if not pilot:
                raise RuntimeError('job does not match any pilot: %s : %s'
                                  % (j.name, j.id))

            pid = pilot['uid']
            self._log.debug('pilot job: %s : %s : %s : %s', 
                            pid, j.id, j.name, j.state)

            # Update the Pilot's state to 'PMGR_ACTIVE_PENDING' if SAGA job
            # submission was successful.  Since the pilot leaves the scope of
            # the PMGR for the time being, we update the complete DB document
            pilot['$all'] = True

            # FIXME: update the right pilot
            with self._pilots_lock:

                self._pilots[pid] = dict()
                self._pilots[pid]['pilot'] = pilot
                self._pilots[pid]['job']   = j

            # make sure we watch that pilot
            with self._check_lock:
                self._checking.append(pid)


    # --------------------------------------------------------------------------
    #
    def _prepare_pilot(self, resource, rcfg, pilot):

        pid = pilot["uid"]
        ret = {'ft' : list(),
               'jd' : None  }

        # ------------------------------------------------------------------
        # Database connection parameters
        sid           = self._session.uid
        database_url  = self._session.dburl

        # ------------------------------------------------------------------
        # pilot description and resource configuration
        number_cores    = pilot['description']['cores']
        runtime         = pilot['description']['runtime']
        queue           = pilot['description']['queue']
        project         = pilot['description']['project']
        cleanup         = pilot['description']['cleanup']
        memory          = pilot['description']['memory']
        candidate_hosts = pilot['description']['candidate_hosts']

        # ------------------------------------------------------------------
        # get parameters from resource cfg, set defaults where needed
        agent_launch_method     = rcfg.get('agent_launch_method')
        agent_dburl             = rcfg.get('agent_mongodb_endpoint', database_url)
        agent_spawner           = rcfg.get('agent_spawner',       DEFAULT_AGENT_SPAWNER)
        rc_agent_config         = rcfg.get('agent_config',        DEFAULT_AGENT_CONFIG)
        agent_scheduler         = rcfg.get('agent_scheduler')
        tunnel_bind_device      = rcfg.get('tunnel_bind_device')
        default_queue           = rcfg.get('default_queue')
        forward_tunnel_endpoint = rcfg.get('forward_tunnel_endpoint')
        lrms                    = rcfg.get('lrms')
        mpi_launch_method       = rcfg.get('mpi_launch_method', '')
        pre_bootstrap_1         = rcfg.get('pre_bootstrap_1', [])
        pre_bootstrap_2         = rcfg.get('pre_bootstrap_2', [])
        python_interpreter      = rcfg.get('python_interpreter')
        task_launch_method      = rcfg.get('task_launch_method')
        rp_version              = rcfg.get('rp_version',          DEFAULT_RP_VERSION)
        virtenv_mode            = rcfg.get('virtenv_mode',        DEFAULT_VIRTENV_MODE)
        virtenv                 = rcfg.get('virtenv',             DEFAULT_VIRTENV)
        cores_per_node          = rcfg.get('cores_per_node', 0)
        health_check            = rcfg.get('health_check', True)
        python_dist             = rcfg.get('python_dist')
        spmd_variation          = rcfg.get('spmd_variation')
        shared_filesystem       = rcfg.get('shared_filesystem', True)
        stage_cacerts           = rcfg.get('stage_cacerts', False)
        cu_pre_exec             = rcfg.get('cu_pre_exec')
        cu_post_exec            = rcfg.get('cu_post_exec')
        export_to_cu            = rcfg.get('export_to_cu')



        # get pilot and global sandbox
        global_sandbox   = self._session._get_global_sandbox (pilot).path
        session_sandbox  = self._session._get_session_sandbox(pilot).path
        pilot_sandbox    = self._session._get_pilot_sandbox  (pilot).path
        pilot['sandbox'] = str(self._session._get_pilot_sandbox(pilot))

        # Agent configuration that is not part of the public API.
        # The agent config can either be a config dict, or
        # a string pointing to a configuration name.  If neither
        # is given, check if 'RADICAL_PILOT_AGENT_CONFIG' is
        # set.  The last fallback is 'agent_default'
        agent_config = pilot['description'].get('_config')
        if not agent_config:
            agent_config = os.environ.get('RADICAL_PILOT_AGENT_CONFIG')
        if not agent_config:
            agent_config = rc_agent_config

        if isinstance(agent_config, dict):
            # nothing to do
            agent_cfg = agent_config
            pass

        elif isinstance(agent_config, basestring):
            try:
                if os.path.exists(agent_config):
                    agent_cfg_file = agent_config

                else:
                    # otherwise interpret as a config name
                    agent_cfg_file = os.path.join(self._conf_dir, "agent_%s.json" % agent_config)

                self._log.info("Read agent config file: %s",  agent_cfg_file)
                agent_cfg = ru.read_json(agent_cfg_file)

                # no matter how we read the config file, we
                # allow for user level overload
                user_cfg_file = '%s/.radical/pilot/config/%s' \
                              % (os.environ['HOME'], os.path.basename(agent_cfg_file))

                if os.path.exists(user_cfg_file):
                    self._log.info("merging user config: %s" % user_cfg_file)
                    user_cfg = ru.read_json(user_cfg_file)
                    ru.dict_merge (agent_cfg, user_cfg, policy='overwrite')

            except Exception as e:
                self._log.exception("Error reading agent config file: %s" % e)
                raise

        else:
            # we can't handle this type
            raise TypeError('agent config must be string (filename) or dict')

        # expand variables in virtenv string
        virtenv = virtenv % {'pilot_sandbox'   :   pilot_sandbox,
                             'session_sandbox' : session_sandbox,
                             'global_sandbox'  :  global_sandbox}

        # Check for deprecated global_virtenv
        if 'global_virtenv' in rcfg:
            raise RuntimeError("'global_virtenv' is deprecated (%s)" % resource)

        # Create a host:port string for use by the bootstrap_1.
        db_url = rs.Url(agent_dburl)
        if db_url.port:
            db_hostport = "%s:%d" % (db_url.host, db_url.port)
        else:
            db_hostport = "%s:%d" % (db_url.host, 27017) # mongodb default

        # ------------------------------------------------------------------
        # the version of the agent is derived from
        # rp_version, which has the following format
        # and interpretation:
        #
        # case rp_version:
        #   @<token>:
        #   @tag/@branch/@commit: # no sdist staging
        #       git clone $github_base radical.pilot.src
        #       (cd radical.pilot.src && git checkout token)
        #       pip install -t $VIRTENV/rp_install/ radical.pilot.src
        #       rm -rf radical.pilot.src
        #       export PYTHONPATH=$VIRTENV/rp_install:$PYTHONPATH
        #
        #   release: # no sdist staging
        #       pip install -t $VIRTENV/rp_install radical.pilot
        #       export PYTHONPATH=$VIRTENV/rp_install:$PYTHONPATH
        #
        #   local: # needs sdist staging
        #       tar zxf $sdist.tgz
        #       pip install -t $VIRTENV/rp_install $sdist/
        #       export PYTHONPATH=$VIRTENV/rp_install:$PYTHONPATH
        #
        #   debug: # needs sdist staging
        #       tar zxf $sdist.tgz
        #       pip install -t $SANDBOX/rp_install $sdist/
        #       export PYTHONPATH=$SANDBOX/rp_install:$PYTHONPATH
        #
        #   installed: # no sdist staging
        #       true
        # esac
        #
        # virtenv_mode
        #   private : error  if ve exists, otherwise create, then use
        #   update  : update if ve exists, otherwise create, then use
        #   create  : use    if ve exists, otherwise create, then use
        #   use     : use    if ve exists, otherwise error,  then exit
        #   recreate: delete if ve exists, otherwise create, then use
        #      
        # examples   :
        #   virtenv@v0.20
        #   virtenv@devel
        #   virtenv@release
        #   virtenv@installed
        #   stage@local
        #   stage@/tmp/my_agent.py
        #
        # Note that some combinations may be invalid,
        # specifically in the context of virtenv_mode.  If, for
        # example, virtenv_mode is 'use', then the 'virtenv:tag'
        # will not make sense, as the virtenv is not updated.
        # In those cases, the virtenv_mode is honored, and
        # a warning is printed.
        #
        # Also, the 'stage' mode can only be combined with the
        # 'local' source, or with a path to the agent (relative
        # to root_dir, or absolute).
        #
        # A rp_version which does not adhere to the
        # above syntax is ignored, and the fallback stage@local
        # is used.

        if  not rp_version.startswith('@') and \
            not rp_version in ['installed', 'local', 'debug', 'release']:
            raise ValueError("invalid rp_version '%s'" % rp_version)

        if rp_version.startswith('@'):
            rp_version  = rp_version[1:]  # strip '@'


        # ------------------------------------------------------------------
        # sanity checks
        if not python_dist        : raise RuntimeError("missing python distribution")
        if not agent_spawner      : raise RuntimeError("missing agent spawner")
        if not agent_scheduler    : raise RuntimeError("missing agent scheduler")
        if not lrms               : raise RuntimeError("missing LRMS")
        if not agent_launch_method: raise RuntimeError("missing agentlaunch method")
        if not task_launch_method : raise RuntimeError("missing task launch method")

        # massage some values
        if not queue :
            queue = default_queue

        if  cleanup and isinstance (cleanup, bool) :
            #  l : log files
            #  u : unit work dirs
            #  v : virtualenv
            #  e : everything (== pilot sandbox)
            if shared_filesystem:
                cleanup = 'luve'
            else:
                # we cannot clean the sandbox from within the agent, as the hop
                # staging would then fail, and we'd get nothing back.
                # FIXME: cleanup needs to be done by the pmgr.launcher, or
                #        someone else, really, after fetching all logs and 
                #        profiles.
                cleanup = 'luv'

            # we never cleanup virtenvs which are not private
            if virtenv_mode is not 'private' :
                cleanup = cleanup.replace ('v', '')

        # add dists to staging files, if needed
        if rp_version in ['local', 'debug']:
            sdist_names = [ru.sdist_name, rs.sdist_name, self._rp_sdist_name]
            sdist_paths = [ru.sdist_path, rs.sdist_path, self._rp_sdist_path]
        else:
            sdist_names = list()
            sdist_paths = list()

        # if cores_per_node is set (!= None), then we need to
        # allocation full nodes, and thus round up
        if cores_per_node:
            cores_per_node = int(cores_per_node)
            number_cores   = int(cores_per_node
                           * math.ceil(float(number_cores)/cores_per_node))

        # set mandatory args
        bootstrap_args  = ""
        bootstrap_args += " -d '%s'" % ':'.join(sdist_names)
        bootstrap_args += " -p '%s'" % pid
        bootstrap_args += " -s '%s'" % sid
        bootstrap_args += " -m '%s'" % virtenv_mode
        bootstrap_args += " -r '%s'" % rp_version
        bootstrap_args += " -b '%s'" % python_dist
        bootstrap_args += " -v '%s'" % virtenv
        bootstrap_args += " -y '%d'" % runtime

        # set optional args
        if lrms == "CCM":           bootstrap_args += " -c"
        if forward_tunnel_endpoint: bootstrap_args += " -f '%s'" % forward_tunnel_endpoint
        if forward_tunnel_endpoint: bootstrap_args += " -h '%s'" % db_hostport
        if python_interpreter:      bootstrap_args += " -i '%s'" % python_interpreter
        if tunnel_bind_device:      bootstrap_args += " -t '%s'" % tunnel_bind_device
        if cleanup:                 bootstrap_args += " -x '%s'" % cleanup

        for arg in pre_bootstrap_1:
            bootstrap_args += " -e '%s'" % arg
        for arg in pre_bootstrap_2:
            bootstrap_args += " -w '%s'" % arg

        # complete agent configuration
        # NOTE: the agent config is really based on our own config, with 
        #       agent specific settings merged in

        agent_base_cfg = copy.deepcopy(self._cfg)
        del(agent_base_cfg['bridges'])    # agent needs separate bridges
        del(agent_base_cfg['components']) # agent needs separate components
        del(agent_base_cfg['number'])     # agent counts differently
        del(agent_base_cfg['heart'])      # agent needs separate heartbeat

        ru.dict_merge(agent_cfg, agent_base_cfg, ru.PRESERVE)

        agent_cfg['owner']              = 'agent_0'
        agent_cfg['cores']              = number_cores
        agent_cfg['debug']              = os.environ.get('RADICAL_PILOT_AGENT_VERBOSE', 
                                                         self._log.getEffectiveLevel())
        agent_cfg['lrms']               = lrms
        agent_cfg['spawner']            = agent_spawner
        agent_cfg['scheduler']          = agent_scheduler
        agent_cfg['runtime']            = runtime
        agent_cfg['pilot_id']           = pid
        agent_cfg['logdir']             = '.'
        agent_cfg['pilot_sandbox']      = pilot_sandbox
        agent_cfg['session_sandbox']    = session_sandbox
        agent_cfg['global_sandbox']     = global_sandbox
        agent_cfg['agent_launch_method']= agent_launch_method
        agent_cfg['task_launch_method'] = task_launch_method
        agent_cfg['mpi_launch_method']  = mpi_launch_method
        agent_cfg['cores_per_node']     = cores_per_node
        agent_cfg['export_to_cu']       = export_to_cu
        agent_cfg['cu_pre_exec']        = cu_pre_exec
        agent_cfg['cu_post_exec']       = cu_post_exec

        # we'll also push the agent config into MongoDB
        pilot['cfg'] = agent_cfg

        # ------------------------------------------------------------------
        # Write agent config dict to a json file in pilot sandbox.

        agent_cfg_name = 'agent_0.cfg'
        cfg_tmp_handle, cfg_tmp_file = tempfile.mkstemp(prefix='rp.agent_cfg.')
        os.close(cfg_tmp_handle)  # file exists now

        # Convert dict to json file
        self._log.debug("Write agent cfg to '%s'.", cfg_tmp_file)
        ru.write_json(agent_cfg, cfg_tmp_file)

        ret['ft'].append({'src' : cfg_tmp_file, 
                          'tgt' : '%s/%s' % (pilot_sandbox, agent_cfg_name),
                          'rem' : True})  # purge the tmp file after packing

        # ----------------------------------------------------------------------
        # we also touch the log and profile tarballs in the target pilot sandbox
        ret['ft'].append({'src' : '/dev/null',
                          'tgt' : '%s/%s' % (pilot_sandbox, '%s.log.tgz' % pid),
                          'rem' : False})  # don't remove /dev/null
        ret['ft'].append({'src' : '/dev/null',
                          'tgt' : '%s/%s' % (pilot_sandbox, '%s.prof.tgz' % pid),
                          'rem' : False})  # don't remove /dev/null

        # check if we have a sandbox cached for that resource.  If so, we have
        # nothing to do.  Otherwise we create the sandbox and stage the RP
        # stack etc.
        # NOTE: this will race when multiple pilot launcher instances are used!
        with self._cache_lock:

            if not resource in self._sandboxes:

                for sdist in sdist_paths:
                    base = os.path.basename(sdist)
                    ret['ft'].append({'src' : sdist, 
                                      'tgt' : '%s/%s' % (session_sandbox, base),
                                      'rem' : False})

                # Copy the bootstrap shell script.
                bootstrapper_path = os.path.abspath("%s/agent/%s" \
                        % (self._root_dir, BOOTSTRAPPER))
                self._log.debug("use bootstrapper %s", bootstrapper_path)

                ret['ft'].append({'src' : bootstrapper_path, 
                                  'tgt' : '%s/%s' % (session_sandbox, BOOTSTRAPPER),
                                  'rem' : False})

                # Some machines cannot run pip due to outdated CA certs.
                # For those, we also stage an updated certificate bundle
                # TODO: use booleans all the way?
                if stage_cacerts:

                    cc_name = 'cacert.pem.gz'
                    cc_path = os.path.abspath("%s/agent/%s" % (self._root_dir, cc_name))
                    self._log.debug("use CAs %s", cc_path)

                    ret['ft'].append({'src' : cc_path, 
                                      'tgt' : '%s/%s' % (session_sandbox, cc_name),
                                      'rem' : False})

                self._sandboxes[resource] = True


        # ------------------------------------------------------------------
        # Create SAGA Job description and submit the pilot job

        jd = rs.job.Description()

        if shared_filesystem:
            bootstrap_tgt = '%s/%s' % (session_sandbox, BOOTSTRAPPER)
        else:
            bootstrap_tgt = '%s/%s' % ('.', BOOTSTRAPPER)

        jd.name                  = pid
        jd.executable            = "/bin/bash"
        jd.arguments             = ['-l %s' % bootstrap_tgt, bootstrap_args]
        jd.working_directory     = pilot_sandbox
        jd.project               = project
        jd.output                = "bootstrap_1.out"
        jd.error                 = "bootstrap_1.err"
        jd.total_cpu_count       = number_cores
        jd.processes_per_host    = cores_per_node
        jd.spmd_variation        = spmd_variation
        jd.wall_time_limit       = runtime
        jd.total_physical_memory = memory
        jd.queue                 = queue
        jd.candidate_hosts       = candidate_hosts
        jd.environment           = dict()

        if 'RADICAL_PILOT_PROFILE' in os.environ :
            jd.environment['RADICAL_PILOT_PROFILE'] = 'TRUE'

        # for condor backends and the like which do not have shared FSs, we add
        # additional staging directives so that the backend system binds the
        # files from the session and pilot sandboxes to the pilot job.
        jd.file_transfer = list()
        if not shared_filesystem:

            jd.file_transfer.extend([
                'site:%s/%s > %s' % (session_sandbox, BOOTSTRAPPER,   BOOTSTRAPPER),
                'site:%s/%s > %s' % (pilot_sandbox,   agent_cfg_name, agent_cfg_name),
                'site:%s/%s.log.tgz > %s.log.tgz' % (pilot_sandbox, pid, pid),
                'site:%s/%s.log.tgz < %s.log.tgz' % (pilot_sandbox, pid, pid)
            ])

            if 'RADICAL_PILOT_PROFILE' in os.environ:
                jd.file_transfer.extend([
                    'site:%s/%s.prof.tgz > %s.prof.tgz' % (pilot_sandbox, pid, pid),
                    'site:%s/%s.prof.tgz < %s.prof.tgz' % (pilot_sandbox, pid, pid)
                ])

            for sdist in sdist_names:
                jd.file_transfer.extend([
                    'site:%s/%s > %s' % (session_sandbox, sdist, sdist)
                ])

            if stage_cacerts:
                jd.file_transfer.extend([
                    'site:%s/%s > %s' % (session_sandbox, cc_name, cc_name)
                ])

        self._log.debug("Bootstrap command line: %s %s" % (jd.executable, jd.arguments))

        ret['jd'] = jd
        return ret


# ------------------------------------------------------------------------------

