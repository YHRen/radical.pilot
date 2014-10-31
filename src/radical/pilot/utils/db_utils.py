
import os
import time
import datetime
import pymongo

import radical.utils as ru


_CACHE_BASEDIR = '/tmp/rp_cache/'


# ------------------------------------------------------------------------------
#
def bson2json (bson_data) :

    # thanks to
    # http://stackoverflow.com/questions/16586180/typeerror-objectid-is-not-json-serializable

    import json
    from   bson.objectid import ObjectId

    class JSONEncoder (json.JSONEncoder) :
        def default (self, o):
            if  isinstance (o, ObjectId) :
                return str (o)
            if  isinstance (o, datetime.datetime) :
                seconds  = time.mktime (o.timetuple ())
                seconds += (o.microsecond / 1000000.0) 
                return seconds
            return json.JSONEncoder.default (self, o)

    return ru.parse_json (JSONEncoder ().encode (bson_data))


# ------------------------------------------------------------------------------
#
def get_session_ids (db) :

    # this is not bein cashed, as the session list can and will change freqently

    cnames = db.collection_names ()
    sids   = list()
    for cname in cnames :
        if  not '.' in cname :
            sids.append (cname)

    return sids


# ------------------------------------------------------------------------------
def get_last_session (db) :

    # this assumes that sessions are ordered by time -- which is the case at
    # this point...
    return get_session_ids (db)[-1]


# ------------------------------------------------------------------------------
def get_session_docs (db, sid, use_cache=True) :

    # session docs may have been cached in /tmp/rp_cache/<sid>.json -- in that
    # case we pull it from there instead of the database, which will be much
    # quicker.  Also, we do cache any retrieved docs to that place, for later
    # use.
    cache = "%s/%s.json" % (_CACHE_BASEDIR, sid)

    if  use_cache and os.path.isfile (cache) :
        return ru.read_json (cache)


    # cache not used or not found -- go to db
    bson_data = dict()

    bson_data['session'] = list(db["%s"    % sid].find ())
    bson_data['pmgr'   ] = list(db["%s.pm" % sid].find ())
    bson_data['pilot'  ] = list(db["%s.p"  % sid].find ())
    bson_data['umgr'   ] = list(db["%s.um" % sid].find ())
    bson_data['unit'   ] = list(db["%s.cu" % sid].find ())

    if  len(bson_data['session']) == 0 :
        raise ValueError ('no such session %s' % sid)

  # if  len(bson_data['session']) > 1 :
  #     print 'more than one session document -- picking first one'

    # there can only be one session, not a list of one
    bson_data['session'] = bson_data['session'][0]

    # we want to add a list of handled units to each pilot doc
    for pilot in bson_data['pilot'] :

        pilot['unit_ids'] = list()

        for unit in bson_data['unit'] :

            if  unit['pilot'] == str(pilot['_id']) :
                pilot['unit_ids'].append (str(unit['_id']))

    # convert bson to json, i.e. serialize the ObjectIDs into strings.
    json_data = bson2json (bson_data)


    # if we got here, we did not find a cached version -- thus add this dataset
    # to the cache
    os.system ('mkdir -p %s' % _CACHE_BASEDIR)
    ru.write_json (json_data, "%s/%s.json" % (_CACHE_BASEDIR, sid))

    return json_data


# ------------------------------------------------------------------------------
def get_session_slothist (db, sid) :
    """
    For all pilots in the session, get the slot lists and slot histories. and
    return as list of tuples like:

            [pilot_id,      [      [hostname, slotnum] ],      [      [slotstate, timestamp] ] ] 
      tuple (string  , list (tuple (string  , int    ) ), list (tuple (string   , datetime ) ) )
    """

    docs = get_session_docs (db, sid)

    ret = list()

    for pilot_doc in docs['pilot'] :

        # slot configuration was only recently (v0.18) added to the RP agent...
        if  not 'slots' in pilot_doc :
            return None

        pid      = str(pilot_doc['_id'])
        slots    =     pilot_doc['slots']
        slothist =     pilot_doc['slothistory']

        slotinfo = list()
        for hostinfo in slots :
            hostname = hostinfo['node'] 
            slotnum  = len (hostinfo['cores'])
            slotinfo.append ([hostname, slotnum])

        ret.append ({'pilot_id' : pid, 
                     'slotinfo' : slotinfo, 
                     'slothist' : slothist})

    return ret


# ------------------------------------------------------------------------------
def get_session_events (db, sid) :
    """
    For all entities in the session, create simple event tuples, and return
    them as a list

           [      [object type, object id, pilot id, timestamp, event name, object document] ]
      list (tuple (string     , string   , string  , datetime , string    , dict           ) )
      
    """

    docs = get_session_docs (db, sid)

    ret = list()

    if  'session' in docs :
        doc   = docs['session']
        odoc  = dict()
        otype = 'session'
        oid   = str(doc['_id'])
        ret.append (['state', otype, oid, None, doc['created'],        'created',        odoc])
        ret.append (['state', otype, oid, None, doc['last_reconnect'], 'last_reconnect', odoc])

    for doc in docs['pilot'] :
        odoc  = dict()
        otype = 'pilot'
        oid   = str(doc['_id'])

        for event in [# 'submitted', 'started',    'finished',  # redundant to states..
                      'input_transfer_started',  'input_transfer_finished', 
                      'output_transfer_started', 'output_transfer_finished'] :
            if  event in doc :
                ret.append (['state', otype, oid, oid, doc[event], event, odoc])
            else : 
                ret.append (['state', otype, oid, oid, None,       event, odoc])

        for event in doc['statehistory'] :
            ret.append (['state',     otype, oid, oid, event['timestamp'], event['state'], odoc])

        if  'callbackhistory' in doc :
            for event in doc['callbackhistory'] :
                ret.append (['callback',  otype, oid, oid, event['timestamp'], event['state'], odoc])


    for doc in docs['unit'] :
        odoc  = dict()
        otype = 'unit'
        oid   = str(doc['_id'])
        pid   = str(doc['pilot'])

        # TODO: change states to look for
        for event in [# 'submitted', 'started',    'finished',  # redundant to states..
                      'input_transfer_started',  'input_transfer_finished', 
                      'output_transfer_started', 'output_transfer_finished'
                      ] :
            if  event in doc :
                ret.append (['state', otype, oid, pid, doc[event], event, doc])
            else :                                
                ret.append (['state', otype, oid, pid, None,       event, doc])

        for event in doc['statehistory'] :
            ret.append (['state',     otype, oid, pid, event['timestamp'], event['state'], doc])

        # TODO: this probably needs to be "doc"
        if  'callbackhistory' in event :
            for event in doc['callbackhistory'] :
                ret.append (['callback',  otype, oid, pid, event['timestamp'], event['state'], doc])

    # we don't want None times, actually
    for r in list(ret) :
        if  r[4] == None :
            ret.remove (r)
    
    ret.sort (key=lambda tup: tup[4]) 

    return ret


# ------------------------------------------------------------------------------

