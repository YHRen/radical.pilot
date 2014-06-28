import os
import sys
import time
import radical.pilot

# READ: The RADICAL-Pilot documentation: 
#   http://radicalpilot.readthedocs.org/en/latest
#
# Try running this example with RADICAL_PILOT_VERBOSE=debug set if 
# you want to see what happens behind the scences!
#


# DBURL defines the MongoDB server URL and has the format mongodb://host:port.
# For the installation of a MongoDB server, refer to http://docs.mongodb.org.
DBURL = os.getenv("RADICAL_PILOT_DBURL")
if DBURL is None:
    print "ERROR: RADICAL_PILOT_DBURL (MongoDB server URL) is not defined."
    sys.exit(1)


#------------------------------------------------------------------------------
#
def pilot_state_cb(pilot, state):
    """pilot_state_change_cb() is a callback function. It gets called very
    time a ComputePilot changes its state.
    """
    print "[Callback]: ComputePilot '{0}' state changed to {1}.".format(
        pilot.uid, state)

    if state == radical.pilot.FAILED:
        print "exit"
        sys.exit(1)

#------------------------------------------------------------------------------
#
def unit_state_change_cb(unit, state):
    """unit_state_change_cb() is a callback function. It gets called very
    time a ComputeUnit changes its state.
    """
    print "[Callback]: ComputeUnit '{0}' state changed to {1}.".format(
        unit.uid, state)
    if state == radical.pilot.FAILED:
        print "            Log: %s" % unit.log

#------------------------------------------------------------------------------
#
if __name__ == "__main__":

    try:
        # Create a new session. A session is the 'root' object for all other
        # RADICAL-Pilot objects. It encapsualtes the MongoDB connection(s) as
        # well as security crendetials.
        session = radical.pilot.Session(database_url=DBURL)
        print "session: %s" % session.uid

        # make jenkins happy
        cred         = radical.pilot.SSHCredential()
        cred.user_id = "merzky"
        session.add_credential (cred)

        # Add a Pilot Manager. Pilot managers manage one or more ComputePilots.
        pmgr = radical.pilot.PilotManager(session=session)

        # Register our callback with the PilotManager. This callback will get
        # called every time any of the pilots managed by the PilotManager
        # change their state.
        pmgr.register_callback(pilot_state_cb)

        # Define 1-core local pilots that run for 10 minutes and clean up
        # after themself.
        pdescriptions = list()
        for i in range (0, 2) :
            pdesc = radical.pilot.ComputePilotDescription()
            pdesc.resource = "india.futuregrid.org"
            pdesc.runtime  = 30 # minutes
            pdesc.cores    = 8*(i+1)
            pdesc.cleanup  = False

            pdescriptions.append(pdesc)


        # Launch the pilots.
        pilots = pmgr.submit_pilots (pdescriptions)
        print "pilots: %s" % pilots

        pilot_ids = list()
        for pilot in pilots :
            pilot_ids.append (pilot.uid)

        pmgr.wait_pilots (pilot_ids, state=radical.pilot.ACTIVE)
        print "pilots: %s" % pilot_ids



        # Create a workload of 8 ComputeUnits (tasks). Each compute unit
        # uses /bin/cat to concatenate two input files, file1.dat and
        # file2.dat. The output is written to STDOUT. cu.environment is
        # used to demonstrate how to set environment variables withih a
        # ComputeUnit - it's not strictly necessary for this example. As
        # a shell script, the ComputeUnits would look something like this:
        #
        #    export INPUT1=file1.dat
        #    export INPUT2=file2.dat
        #    /bin/cat $INPUT1 $INPUT2
        #
        cu_descriptions = []

        for unit_count in range(0, 100):
            cu = radical.pilot.ComputeUnitDescription()
            cu.environment = {"INPUT1": "file1.dat", "INPUT2": "file2.dat"}
            cu.executable  = "/bin/sh"
            cu.arguments   = ["-l", "-c", "sleep 5; echo \"$INPUT1 $INPUT2\""]
            cu.cores       = 1
          # cu.input_data  = ["./file1.dat", "./file2.dat"]

            cu_descriptions.append(cu)

        # Combine the ComputePilot, the ComputeUnits and a scheduler via
        # a UnitManager object.
        umgr = radical.pilot.UnitManager(
            session=session,
            scheduler=radical.pilot.SCHED_ROUND_ROBIN)

        # Register our callback with the UnitManager. This callback will get
        # called every time any of the units managed by the UnitManager
        # change their state.
        umgr.register_callback(unit_state_change_cb)

        # Add the previsouly created ComputePilots to the UnitManager.
        umgr.add_pilots(pilots)

        # Submit the previously created ComputeUnit descriptions to the
        # PilotManager. This will trigger the selected scheduler to start
        # assigning ComputeUnits to the ComputePilots.
        units = umgr.submit_units(cu_descriptions)
        print "units: %s" % umgr.list_units ()

        # Wait for all compute units to reach a terminal state (DONE or FAILED).
        umgr.wait_units()

        for unit in units:
            print "* Task %s (executed @ %s) state %s, exit code: %s, started: %s, finished: %s" \
                % (unit.uid, unit.execution_locations, unit.state, unit.exit_code, unit.start_time, unit.stop_time)

        # Close automatically cancels the pilot(s).
        print session.uid
        pmgr.cancel_pilots ()
        time.sleep (3)

        sid = session.uid
        session.close(delete=False)

        # run the stats plotter
        os.system ("bin/radicalpilot-stats -m plot -s %s" % sid) 
        os.system ("cp -v %s.png report/rp.benchmark.png" % sid) 

        sys.exit(0)

    except radical.pilot.PilotException, ex:
        # Catch all exceptions and exit with and error.
        print "Error during execution: %s" % ex
        sys.exit(1)

