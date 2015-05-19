#!/usr/bin/env python

""" sse.py: Save Store Extract

Defines the thread classes that handle storing frames and metaframes to db
"""
__name__ = 'sse'
__license__ = 'GPL v3.0'
__version__ = '0.0.1'
__date__ = 'May 2015'
__author__ = 'Dale Patterson'
__maintainer__ = 'Dale Patterson'
__email__ = 'wraith.wireless@yandex.com'
__status__ = 'Development'

import os                                   # path/file functions
import threading                            # sync items
import psycopg2 as psql                     # postgresql api
from dateutil import parser as dtparser     # parse out timestamps
import wraith.radio.radiotap as rtap        # 802.11 layer 1 parsing
from wraith.radio import mpdu               # 802.11 layer 2 parsing
from wraith.radio import mcs                # mcs functions
from wraith.radio import channels           # 802.11 channels/RFs
from wraith.radio.oui import manufacturer   # oui functions
from wraith.utils import simplepcap as pcap # write frames to file
import wraith.nidus.nidusdb as ndb          # for exceptions

# Task Definitions

class SaveTask(tuple):
    # noinspection PyInitNewSignature
    def __new__(cls,ts,fid,sid,mac,frame,lr):
        """
         ts - timestamp
         fid - frame id
         sid - session id
         mac - capture source mac
         frame - actual frame
         lr - left/right offsets into layer 1 and layer 2
        """
        return super(SaveTask,cls).__new__(cls,tuple([ts,fid,sid,mac,frame,lr]))
    @property
    def ts(self): return self[0]
    @property
    def fid(self): return self[1]
    @property
    def sid(self): return self[2]
    @property
    def mac(self): return self[3]
    @property
    def frame(self): return self[4]
    @property
    def offsets(self): return self[5]

class StoreTask(tuple):
    # noinspection PyInitNewSignature
    def __new__(cls,fid,mac,l1,l2):
        """
         fid - frame id
         mac - capture source mac
         l1 - radiotap dict
         l2 - mpdu dict
        """
        return super(StoreTask,cls).__new__(cls,tuple([fid,mac,l1,l2]))
    @property
    def fid(self): return self[0]
    @property
    def mac(self): return self[1]
    @property
    def l1(self): return self[2]
    @property
    def l2(self): return self[3]

class ExtractTask(tuple):
    # noinspection PyInitNewSignature
    def __new__(cls,ts,fid,l2):
        """
         ts - timestamp
         fid - frame id
         l2 - MPDU dict
        """
        return super(ExtractTask,cls).__new__(cls,tuple([ts,fid,l2]))
    @property
    def ts(self): return self[0]
    @property
    def fid(self): return self[1]
    @property
    def l2(self): return self[2]

# Worker definitions

class SSEThread(threading.Thread):
    """ Super Class for a SaveStoreExtract worker thread """
    def __init__(self,tasks,h,p,db,u,pwd):
        """
         tasks: queue of tasks to pull from
         h: database host
         p: database port
         db: database name
         u: user
         pwd: user
        """
        threading.Thread.__init__(self)
        self._qT = tasks
        self._conn = None # connection to database
        self._err = None  # internal err information
        try:
            self._conn = psql.connect(host=h,port=p,dbname=db,user=u,password=pwd)
            curs = self._conn.cursor()
            curs.execute("set time zone 'UTC';")
            curs.close()
            self._conn.commit()
        except psql.OperationalError as e:
            if e.__str__().find('connect') > 0:
                raise ndb.NidusDBServerException("Postgresql not running")
            elif e.__str__().find('authentication') > 0:
                raise ndb.NidusDBAuthException("Invalid connection string")
            else:
                raise ndb.NidusDBException("Database failure: %s" % e)

    def run(self):
        """ processing loop - calls consume until told to stop """
        while True:
            item = self._qT.get()
            if item == '!STOP!': break
            try:
                self._consume(item)
            except ndb.NidusDBSubmitException as e:
                print "DB Error: ", e
            except Exception as e:
                print "Error: ", e
        self._clean() # cleanup

    def _clean(self):
        """ closes database and call _cleanup for any subthread closing taks """
        if self._conn and not self._conn.closed: self._conn.close()
        self._cleanup()

    # the following must be implemented by subclasses
    def _consume(self,item): raise NotImplementedError
    def _cleanup(self): raise NotImplementedError

class SaveThread(SSEThread):
    """ handles saving the frame in tasks queue to file """
    def __init__(self,tasks,path,private,sz,db):
        """
         path: directory to store pcaps in
         private: {no = save entire frame|yes = save only layer 1 and 2}
         sz: max size of pcap file
         db: datbase connection tuple t = (h=host,p=port,db=dbname,u=user,pwd=db pwd)
        """
        SSEThread.__init__(self,tasks,db[0],db[1],db[2],db[3],db[4])
        self._path = path
        self._private = private
        self._sz = sz
        self._pkts = []
        self._fout = None

    def _cleanup(self):
        """ close file if it is open """
        # write any stored packets and close opened file
        try:
            if self._pkts: self._writepkts()
            if self._fout: self._fout.close()
        except:
            # blanket exception to catch everything on quit
            pass

    def _consume(self,item):
        """ write the packet in item to file """
        sid = item.sid
        mac = item.mac
        frame = item.frame
        (left,right) = item.offsets

        # save the packet as tuple (time,frameid,frame) for later writing
        if self._private and left < right: frame = frame[:left] + frame[right:]
        self._pkts.append((item.ts,item.fid,frame))

        # if there are (hardcoded: 20) number of stored frames, write them out
        if len(self._pkts) > 20:
            # close any open file exceeding specified size
            if self._fout and os.path.getsize(self._fout.name) > self._sz:
                self._fout.close()
                self._fout = None

            # if no file, open a new file (using ts from first pkt in name)
            if not self._fout:
                fname = os.path.join(self._path,
                                     "%d_%s_%s.pcap" % (sid,self._pkts[0][0],mac))
                try:
                    self._fout = pcap.pcapopen(fname)
                except pcap.PCAPException as e:
                    raise ndb.NidusDBSubmitException(e)

            self._writepkts()

    def _writepkts(self):
        """ write stored packets to file """
        curs = None
        try:
            # we'll catch db errors in the internal loop and attempt to
            # continue writing
            curs = self._conn.cursor()
            for pkt in self._pkts:
                sql = "insert into frame_path (fid,filepath) values (%s,%s);"
                try:
                    curs.execute(sql,(pkt[1],os.path.split(self._fout.name)[1]))
                except psql.Error as e:
                    print 'error writing frame_path', e
                    self._conn.rollback()
                else:
                    self._conn.commit()
                pcap.pktwrite(self._fout,pkt[0],pkt[2])
        except pcap.PCAPException:
            self._fout.close()
            self._fout = None
        finally:
            # make sure to reset packet list and close the cursor
            self._pkts = []
            curs.close()

class StoreThread(SSEThread):
    """ stores the frame details in the task queue to the db """
    def __init__(self,tasks,sid,db):
        """
         tasks: the tasks queue
         sid: session id
         db: datbase connection tuple t = (h=host,p=port,db=dbname,u=user,pwd=db pwd)
        """
        SSEThread.__init__(self,tasks,db[0],db[1],db[2],db[3],db[4])
        self._sid = sid

    def _cleanup(self): pass

    def _consume(self,item):
        # get our cursor & extract vars from item
        curs = self._conn.cursor()
        fid = item.fid
        rdo = item.mac
        dR = item.l1
        dM = item.l2

        # each _insert function will reraise psql related errors after
        # setting the internal err tuple
        try:
            # insert radiotap data
            if 'a-mpdu' in dR['present']: self._insertampdu(fid,dR,curs)
            self._insertsource(fid,rdo,dR,curs)
            self._insertsignal(fid,dR,curs)

            # insert mpdu data
            if dM.offset > 0:
                self._inserttraffic(fid,dM,curs)
                if dM.qosctrl: self._insertqos(fid,dM,curs)
                if dM.crypt: self._insertcrypt(fid,dM,curs)
        except psql.Error as e:
            self._conn.rollback()
            curs.close()
            raise ndb.NidusDBSubmitException(e.pgcode,e.pgerror,self._err[0],self._err[1])
        else:
            self._conn.commit()
        finally:
            curs.close()

    def _insertampdu(self,fid,r,curs):
        """
         insert ampdu from frame fid (as defined in radiotap r) in db using
         cursor curs
        """
        try:
            sql = "insert into ampdu fid,refnum,flags) values (%s,%s,%s);"
            curs.execute(sql,(fid,r['a-mpdu'][0],r['a-mpdu'][1]))
        except psql.Error:
            self._err = ('ampdu',fid)
            raise

    def _insertsource(self,fid,rdo,r,curs):
        """
         insert source from frame fid (as defined in radiotap r), collected by
         rdo in db using cursor curs
        """
        try:
            ant = r['antenna'] if 'antenna' in r else 0
            pwr = r['antsignal'] if 'antsignal' in r else 0
            sql = "insert into source (fid,src,antenna,rfpwr) values (%s,%s,%s,%s);"
            curs.execute(sql,(fid,rdo,ant,pwr))
        except psql.Error:
            self._err = ('source',fid)
            raise

    def _insertsignal(self,fid,r,curs):
        """
         insert signal record from frame fid (as defined in radiotap r) in db
         using cursors curs
        """
        try:
            # determine what standard, the data rate and any mcs fields
            # assuming channels will always be present in radiotap r
            try:
                std = 'n'
                mcsflags = rtap.mcsflags_params(r['mcs'][0],r['mcs'][1])
                if mcsflags['bw'] == rtap.MCS_BW_20: bw = '20'
                elif mcsflags['bw'] == rtap.MCS_BW_40: bw = '40'
                elif mcsflags['bw'] == rtap.MCS_BW_20L: bw = '20L'
                else: bw = '20U'
                width = 40 if bw == '40' else 20
                gi = 1 if 'gi' in mcsflags and mcsflags['gi'] > 0 else 0
                ht = 1 if 'ht' in mcsflags and mcsflags['ht'] > 0 else 0
                index = r['mcs'][2]
                rate = mcs.mcs_rate(index,width,gi)
                hasMCS = 1
            except:
                if r['channel'][0] in channels.ISM_24_F2C:
                    if rtap.chflags_get(r['channel'][1],'cck'):
                        std = 'b'
                    else:
                        std = 'g'
                else:
                    std = 'a'
                rate = r['rate'] * 0.5 if 'rate' in r else 0
                bw = None
                gi = None
                ht = None
                index = None
                hasMCS = 0
            sql = """
                   insert into signal (fid,std,rate,channel,chflags,rf,ht,
                                       mcs_bw,mcs_gi,mcs_ht,mcs_index)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
                  """
            curs.execute(sql,(fid,std,rate,channels.f2c(r['channel'][0]),
                              r['channel'][1],r['channel'][0],hasMCS,bw,gi,
                              ht,index))
        except psql.Error:
            self._err = ('signal',fid)
            raise

    def _inserttraffic(self,fid,m,curs):
        """
         insert traffic from frame fid (defined in mpdu m) into db using
         cursor curs
        """
        try:
            # get out duration type and value
            dVal = None
            if m.duration['type'] == 'vcs': dVal = m.duration['dur']
            elif m.duration['type'] == 'aid': dVal = m.duration['aid']

            sql = """
                   insert into traffic (fid,type,subtype,td,fd,mf,rt,pm,md,pf,so,
                                        dur_type,dur_val,addr1,addr2,addr3,
                                        fragnum,seqnum,addr4,crypt)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
                  """
            curs.execute(sql,(fid,mpdu.FT_TYPES[m.type],
                                  mpdu.subtypes(m.type,m.subtype),
                                  m.flags['td'],m.flags['fd'],
                                  m.flags['mf'],m.flags['r'],
                                  m.flags['pm'],m.flags['md'],
                                  m.flags['pf'],m.flags['o'],
                                  m.duration['type'],dVal,
                                  m.addr1,m.addr2,m.addr3,
                                  m.seqctrl['fragno'] if m.seqctrl else None,
                                  m.seqctrl['seqno'] if m.seqctrl else None,
                                  m.addr4,
                                  m.crypt['type'] if m.crypt else 'none',))
        except psql.Error:
            self._err = ('traffic',fid)
            raise

    def _insertqos(self,fid,m,curs):
        """
         inserts the qos ctrl from frame id (defined in mpdu m) into the db
         using cursor curs
        """
        try:
            sql = """
                   insert into qosctrl (fid,tid,eosp,ackpol,amsdu,txop)
                   values (%s,%s,%s,%s,%s,%s);
                  """
            curs.execute(sql,(fid,m.qosctrl['tid'],
                                  m.qosctrl['eosp'],
                                  m.qosctrl['ack-policy'],
                                  m.qosctrl['a-msdu'],
                                  m.qosctrl['txop']))
        except psql.Error:
            self._err = ('qosctrl',fid)
            raise

    def _insertcrypt(self,fid,m,curs):
        """
         insert the encryption scheme/data from frame fid (as defined in mpdu m)
         into the db using the cursor curs
        """
        try:
            if m.crypt['type'] == 'wep':
                sql = """
                       insert into wepcrypt (fid,iv,key_id,icv) values (%s,%s,%s,%s);
                      """
                curs.execute(sql,(fid,m.crypt['iv'],
                                      m.crypt['key-id'],
                                      m.crypt['icv']))
            elif m.crypt['type'] == 'tkip':
                sql = """
                       insert into tkipcrypt (fid,tsc1,wepseed,tsc0,key_id,
                                              tsc2,tsc3,tsc4,tsc5,mic,icv)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
                      """
                curs.execute(sql,(fid,m.crypt['iv']['tsc1'],
                                      m.crypt['iv']['wep-seed'],
                                      m.crypt['iv']['tsc0'],
                                      m.crypt['iv']['key-id']['key-id'],
                                      m.crypt['ext-iv']['tsc2'],
                                      m.crypt['ext-iv']['tsc3'],
                                      m.crypt['ext-iv']['tsc4'],
                                      m.crypt['ext-iv']['tsc5'],
                                      m.crypt['mic'],
                                      m.crypt['icv']))
            elif m.crypt['type'] == 'ccmp':
                sql = """
                       insert into ccmpcrypt (fid,pn0,pn1,key_id,pn2,
                                              pn3,pn4,pn5,mic)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s);
                      """
                curs.execute(sql,(fid,m.crypt['pn0'],
                                      m.crypt['pn1'],
                                      m.crypt['key-id']['key-id'],
                                      m.crypt['pn2'],
                                      m.crypt['pn3'],
                                      m.crypt['pn4'],
                                      m.crypt['pn5'],
                                      m.crypt['mic']))
            else:
                # undefined crypt type
                pass
        except psql.Error:
            self._err = ('crypt',fid)
            raise

class ExtractThread(SSEThread):
    """
     the ExtractThread exposes data that can be inferred from other tables IOT
     to facilitate analysis by decreasing the need for complex queries.
     Extracts & stores the details of nets/stas etc from mpdus
    """
    def __init__(self,tasks,lSta,sid,oui,db):
        """
         lSta - lock on the stay dictionary
         sid - session id
         oui - oui dict
         db: datbase connection tuple t = (h=host,p=port,db=dbname,u=user,pwd=db pwd)
        """
        SSEThread.__init__(self,tasks,db[0],db[1],db[2],db[3],db[4])
        self._l = lSta
        self._sid = sid
        self._oui = oui

    def _cleanup(self): pass

    def _consume(self,item):
        # make a cursor and extract variables
        curs = self._conn.cursor()
        ts = item.ts
        fid = item.fid
        l2 = item.l2

        # sta addresses
        # Function To DS From DS Address 1 Address 2 Address 3 Address 4
        # IBSS/Intra   0    0        RA=DA     TA=SA     BSSID       N/A
        # From AP      0    1        RA=DA  TA=BSSID        SA       N/A
        # To AP        1    0     RA=BSSID     TA=SA        DA       N/A
        # Wireless DS  1    1     RA=BSSID  TA=BSSID        DA=WDS    SA=WDS

        # extract unique addresses of stas in the mpdu into the addr dict having
        # the form <hwaddr>->{'loc:[<i..n>],'id':<sta_id>} where i=1 through 4
        addrs = {}
        locations = ['addr1','addr2','addr3','addr4']
        for i in xrange(len(locations)):
            a = locations[i]
            if not a in l2: break         # no more stas to process
            if l2[a] in addrs:
                addrs[l2[a]]['loc'].append(i+1)
            else:
                addrs[l2[a]] = {'loc':[i+1],'id':None}

        # each _insert function will reraise psql related errors after
        # setting the internal err tuple
        try:
            # NOTE: 1) insertsta modifies the addrs dict in place, assigning
            # ids to each nonbroadcast address
            # 2) insertsta/insertsta_activity also commits the tranaction after
            # each insert (in the internal loops)
            # 3) Each of the insertsta and insertsta_activity use the shared lock
            # to ensure ts and id data is written correctly
            self._insertsta(fid,ts,addrs,curs)
            self._insertsta_activity(fid,ts,addrs,curs)

            # we will process management frames
            if l2.type == mpdu.FT_MGMT:
                if l2.subtype == mpdu.ST_MGMT_ASSOC_REQ:
                    self._insertassocreq(fid,addrs[l2.addr2]['id'],
                                         addrs[l2.addr1]['id'],l2,curs)
                elif l2.subtype == mpdu.ST_MGMT_ASSOC_RESP:
                    self._insertassocresp('assoc',fid,addrs[l2.addr2]['id'],
                                          addrs[l2.addr1]['id'],l2,curs)
                elif l2.subtype == mpdu.ST_MGMT_REASSOC_REQ:
                    self._insertreassocreq(fid,ts,addrs[l2.addr2]['id'],
                                           addrs[l2.addr1]['id'],l2,curs)
                elif l2.subtype == mpdu.ST_MGMT_REASSOC_RESP:
                    self._insertassocresp('reassoc',fid,addrs[l2.addr2]['id'],
                                          addrs[l2.addr1]['id'],l2,curs)
                elif l2.subtype == mpdu.ST_MGMT_PROBE_REQ:
                    self._insertprobereq(fid,addrs[l2.addr2]['id'],
                                         addrs[l2.addr3]['id'],l2,curs)
                elif l2.subtype == mpdu.ST_MGMT_PROBE_RESP:
                    self._insertproberesp(fid,addrs[l2.addr2]['id'],
                                         addrs[l2.addr1]['id'],l2,curs)
                #elif l2.subtype == mpdu.ST_MGMT_TIMING_ADV: pass
                elif l2.subtype == mpdu.ST_MGMT_BEACON:
                    self._insertbeacon(fid,addrs[l2.addr2]['id'],l2,curs)
                #elif l2.subtype == mpdu.ST_MGMT_ATIM: pass
                elif l2.subtype == mpdu.ST_MGMT_DISASSOC:
                    self._insertdisassaoc(fid,addrs[l2.addr1]['id'],
                                          addrs[l2.addr2]['id'],l2,curs)
                elif l2.subtype == mpdu.ST_MGMT_AUTH:
                    self._insertauth(fid,addrs[l2.addr1]['id'],
                                     addrs[l2.addr2]['id'],l2,curs)
                elif l2.subtype == mpdu.ST_MGMT_DEAUTH:
                    self._insertdeauth(fid,addrs[l2.addr1]['id'],
                                       addrs[l2.addr2]['id'],l2,curs)
                elif l2.subtype == mpdu.ST_MGMT_ACTION:
                    self._insertaction(fid,addrs[l2.addr1]['id'],addrs[l2.addr2]['id'],
                                       addrs[l2.addr3]['id'],False,l2,curs)
                elif l2.subtype == mpdu.ST_MGMT_ACTION_NOACK:
                    self._insertaction(fid,addrs[l2.addr1]['id'],addrs[l2.addr2]['id'],
                                       addrs[l2.addr3]['id'],True,l2,curs)
        except psql.Error as e:
            self._conn.rollback()
            raise ndb.NidusDBSubmitException(e.pgcode,e.pgerror,self._err[0],self._err[1])
        else:
            self._conn.commit()
        finally:
            curs.close()

    def _insertsta(self,fid,ts,addrs,curs):
        """
         given the frame id fid with timestamp ts and address dict of stas in
         the frame, inserts unique sta's and sta activity in db using the
         cursor curs
         NOTE: as a side-effect will insert the sta id for each non-broadcast
          address
        """
        #### ENTER sta CS
        try:
            # do not process broadcast addresses
            nonbroadcast = [addr for addr in addrs if addr != mpdu.BROADCAST]
            if nonbroadcast:
                sql = "select id, mac from sta where "
                sql += " or ".join(["mac=%s" for _ in nonbroadcast])
                sql += ';'

                self._l.acquire()

                # get all rows with current sta(s) & add sta id if it is not new
                curs.execute(sql,tuple(nonbroadcast))
                for row in curs.fetchall():
                    if row[1] in addrs: addrs[row[1]]['id'] = row[0]

                # for each address
                for addr in nonbroadcast:
                    # if this one doesnt have an id (not seen before) add it
                    if not addrs[addr]['id']:
                        # insert the new sta - commit the transaction because
                        # we we're experiencing duplicate key issues w/o commit
                        sql = """
                               insert into sta (sid,fid,spotted,mac,manuf)
                               values (%s,%s,%s,%s,%s) RETURNING id;
                              """
                        curs.execute(sql,(self._sid,fid,ts,addr,
                                          manufacturer(self._oui,addr)))
                        addrs[addr]['id'] = curs.fetchone()[0]
                        self._conn.commit()
        except psql.Error:
            # tag the error & reraise (letting the finally block release the lock)
            self._err = ('sta',fid)
            raise # reraise
        finally:
            self._l.release()
            #### EXIT CS

    def _insertsta_activity(self,fid,ts,addrs,curs):
        """
         inserts sta activity of sta in addrs at timestamp ts into db using
         the cursor curs
        """
        try:
            # don't process broadcast addresses
            nonbroadcast = [addr for addr in addrs if addr != mpdu.BROADCAST]

            #### ENTER CS
            self._l.acquire()

            for addr in nonbroadcast:
                # check sta activity, addr2 is transmitting
                mode = 'tx' if 2 in addrs[addr]['loc'] else 'rx'
                fs = ls = fh = lh = None # each ts will be initially None

                sql = """
                       select firstSeen,lastSeen,firstHeard,lastHeard
                       from sta_activity where sid=%s and staid=%s;
                      """
                curs.execute(sql,(self._sid,(addrs[addr]['id']),))
                row = curs.fetchone()

                if row:
                    # sta has been seen this session
                    # NOTE: frame ts needs to be converted to a datetime
                    # object w/ tz info before being compared to db's ts
                    tstz = dtparser.parse(ts+"+00:00")
                    if mode == 'tx':
                        # check 'heard' activities
                        if not row[2]:
                            # 1st tx heard by sta, update first/lastHeard
                            fh = lh = ts
                        elif tstz > row[3]:
                            # tx is later than lastHeard, update it
                            lh = ts
                    else:
                        # check 'seen' activities
                        if not row[0]:
                            # 1st time sta seen, update first/lastSeen
                            fs = ls = ts
                        elif tstz > row[1]:
                            # ts is later than lastSeen, update it
                            ls = ts

                    # build query if there is something to update
                    # commit the transaction because we were experiencing
                    # duplicate key issues w/o commit
                    us = ""
                    vs = []
                    if fs or ls or fh or lh:
                        if fs:
                            us = " firstSeen=%s"
                            vs.append(fs)
                        if ls:
                            if not us: us = " lastSeen=%s"
                            else: us += ", lastSeen=%s"
                            vs.append(ls)
                        if fh:
                            if not us: us = " firstHeard=%s"
                            else: us += ", firstHeard=%s"
                            vs.append(fh)
                        if lh:
                            if not us: us = " lastHeard=%s"
                            else: us += ", lastHeard=%s"
                            vs.append(lh)
                        sql = "update sta_activity set"
                        sql += us
                        sql += " where sid=%s and staid=%s;"
                        curs.execute(sql,tuple(vs+[self._sid,addrs[addr]['id']]))
                        self._conn.commit()
                else:
                    # sta has not been seen this session
                    # commit the transaction because we were experiencing
                    # duplicate key issues w/o commit
                    if mode == 'tx':
                        fh = lh = ts
                    else:
                        fs = ls = ts
                    sql = """
                           insert into sta_activity (sid,staid,firstSeen,lastSeen,
                                                     firstHeard,lastHeard)
                           values (%s,%s,%s,%s,%s,%s);
                          """
                    curs.execute(sql,(self._sid,addrs[addr]['id'],fs,ls,fh,lh))
                    self._conn.commit()
        except psql.Error:
            # tag the error and reraise, finally block will release the CS lock
            self._err = ('sta_activity',fid)
        finally:
            self._l.release()
            #### EXIT CS

    def _insertassocreq(self,fid,client,ap,l2,curs):
        """
         inserts the association req from the sta with id client to the ap with
         id ap, seen in frame fid w/ further details in l2 into the db using the
         cursors curs
        """
        try:
            sql = """
                   insert into assocreq (fid,client,ap,ess,ibss,cf_pollable,
                                         cf_poll_req,privacy,short_pre,pbcc,
                                         ch_agility,spec_mgmt,qos,short_slot,
                                         apsd,rdo_meas,dsss_ofdm,del_ba,imm_ba,
                                         listen_int,ssid,sup_rates,ext_rates,vendors)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
                  """
            ssid = None
            sup_rs = []
            ext_rs = []
            vendors = []
            for ie in l2.info_els:
                if ie[0] == mpdu.EID_SSID: ssid = ie[1]
                if ie[0] == mpdu.EID_SUPPORTED_RATES: sup_rs = ie[1]
                if ie[0] == mpdu.EID_EXTENDED_RATES: ext_rs = ie[1]
                if ie[0] == mpdu.EID_VEND_SPEC: vendors.append(ie[1][0])
            curs.execute(sql,(fid,client,ap,
                              l2.fixed_params['capability']['ess'],
                              l2.fixed_params['capability']['ibss'],
                              l2.fixed_params['capability']['cfpollable'],
                              l2.fixed_params['capability']['cf-poll-req'],
                              l2.fixed_params['capability']['privacy'],
                              l2.fixed_params['capability']['short-pre'],
                              l2.fixed_params['capability']['pbcc'],
                              l2.fixed_params['capability']['ch-agility'],
                              l2.fixed_params['capability']['spec-mgmt'],
                              l2.fixed_params['capability']['qos'],
                              l2.fixed_params['capability']['time-slot'],
                              l2.fixed_params['capability']['apsd'],
                              l2.fixed_params['capability']['rdo-meas'],
                              l2.fixed_params['capability']['dsss-ofdm'],
                              l2.fixed_params['capability']['delayed-ba'],
                              l2.fixed_params['capability']['immediate-ba'],
                              l2.fixed_params['listen-int'],
                              ssid,sup_rs,ext_rs,vendors))
        except psql.Error:
            self._err = ('assocreq',fid)
            raise
        except (ValueError,IndexError,AttributeError):
            self._err = ('assocreq',fid)
            raise

    def _insertreassocreq(self,fid,ts,client,ap,l2,curs):
        """
         inserts the reassociation req from the sta with id client to the ap with
         id ap, seen in frame fid w/ further details in l2 into the db using the
         cursors curs
        """
        try:
            # lock the internal to determine if a) we have a matching sta for
            # cur ap or if we need to add one
            try:
                #### ENTER CS
                self._l.acquire()
                curs.execute("select id from sta where mac=%s;",
                             (l2.fixed_params['current-ap'],))
                row = curs.fetchone()
                if row: curid = row[0]
                else:
                    # not present, need to add it
                    sql = """
                           insert into sta (sid,fid,spotted,mac,manuf)
                           values (%s,%s,%s,%s,%s) RETURNING id;
                          """
                    curs.execute(sql,(self._sid,fid,ts,l2.fixed_params['current-ap'],
                                          manufacturer(self._oui,l2.fixed_params['current-ap'])))
                    curid = curs.fetchone()[0]
                    self._conn.commit()
            except psql.Error:
                self._err = ('reassoc',fid)
                raise
            finally:
                self._l.release()
                #### EXIT CS

            sql = """
                   insert into reassocreq (fid,client,ap,ess,ibss,cf_pollable,
                                           cf_poll_req,privacy,short_pre,pbcc,
                                           ch_agility,spec_mgmt,qos,short_slot,
                                           apsd,rdo_meas,dsss_ofdm,del_ba,imm_ba,
                                           listen_int,cur_ap,ssid,sup_rates,ext_rates,
                                           vendors)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
                  """
            ssid = None
            sup_rs = []
            ext_rs = []
            vendors = []
            for ie in l2.info_els:
                if ie[0] == mpdu.EID_SSID: ssid = ie[1]
                if ie[0] == mpdu.EID_SUPPORTED_RATES: sup_rs = ie[1]
                if ie[0] == mpdu.EID_EXTENDED_RATES: ext_rs = ie[1]
                if ie[0] == mpdu.EID_VEND_SPEC: vendors.append(ie[1][0])
            curs.execute(sql,(fid,client,ap,
                              l2.fixed_params['capability']['ess'],
                              l2.fixed_params['capability']['ibss'],
                              l2.fixed_params['capability']['cfpollable'],
                              l2.fixed_params['capability']['cf-poll-req'],
                              l2.fixed_params['capability']['privacy'],
                              l2.fixed_params['capability']['short-pre'],
                              l2.fixed_params['capability']['pbcc'],
                              l2.fixed_params['capability']['ch-agility'],
                              l2.fixed_params['capability']['spec-mgmt'],
                              l2.fixed_params['capability']['qos'],
                              l2.fixed_params['capability']['time-slot'],
                              l2.fixed_params['capability']['apsd'],
                              l2.fixed_params['capability']['rdo-meas'],
                              l2.fixed_params['capability']['dsss-ofdm'],
                              l2.fixed_params['capability']['delayed-ba'],
                              l2.fixed_params['capability']['immediate-ba'],
                              l2.fixed_params['listen-int'],
                              curid,ssid,sup_rs,ext_rs,vendors))
        except psql.Error:
            self._err = ('reassocreq',fid)
            raise
        except (ValueError,IndexError,AttributeError):
            self._err = ('reassocreq',fid)
            raise

    def _insertassocresp(self,assoc,fid,ap,client,l2,curs):
        """
         inserts the (re)association resp from ap to client seen in frame fid w/
         further details in l2 into the db using the cursors curs Since both
         association response and reassociation response have the same format,
         they are saved to the same table identified by the type 'assoc'
        """
        try:
            sql = """
                   insert into assocresp (fid,client,ap,type,ess,ibss,cf_pollable,
                                          cf_poll_req,privacy,short_pre,pbcc,
                                          ch_agility,spec_mgmt,qos,short_slot,
                                          apsd,rdo_meas,dsss_ofdm,del_ba,imm_ba,
                                          status,aid,ssid,sup_rates,ext_rates,vendors)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
                  """
            aid = l2.fixed_params['status-code'] if 'status-code' in l2.fixed_params else None
            ssid = None
            sup_rs = []
            ext_rs = []
            vendors = []
            for ie in l2.info_els:
                if ie[0] == mpdu.EID_SSID: ssid = ie[1]
                if ie[0] == mpdu.EID_SUPPORTED_RATES: sup_rs = ie[1]
                if ie[0] == mpdu.EID_EXTENDED_RATES: ext_rs = ie[1]
                if ie[0] == mpdu.EID_VEND_SPEC: vendors.append(ie[1][0])
            curs.execute(sql,(fid,client,ap,assoc,
                              l2.fixed_params['capability']['ess'],
                              l2.fixed_params['capability']['ibss'],
                              l2.fixed_params['capability']['cfpollable'],
                              l2.fixed_params['capability']['cf-poll-req'],
                              l2.fixed_params['capability']['privacy'],
                              l2.fixed_params['capability']['short-pre'],
                              l2.fixed_params['capability']['pbcc'],
                              l2.fixed_params['capability']['ch-agility'],
                              l2.fixed_params['capability']['spec-mgmt'],
                              l2.fixed_params['capability']['qos'],
                              l2.fixed_params['capability']['time-slot'],
                              l2.fixed_params['capability']['apsd'],
                              l2.fixed_params['capability']['rdo-meas'],
                              l2.fixed_params['capability']['dsss-ofdm'],
                              l2.fixed_params['capability']['delayed-ba'],
                              l2.fixed_params['capability']['immediate-ba'],
                              l2.fixed_params['status-code'],aid,
                              ssid,sup_rs,ext_rs,vendors))
        except psql.Error:
            self._err = ('assocresp',fid)
            raise
        except (ValueError,IndexError,AttributeError):
            self._err = ('assocresp',fid)
            raise

    def _insertprobereq(self,fid,client,ap,l2,curs):
        """
         inserts a probe request from the sta with id client, seen in frame id
         at timestamp ts with details in l2 into the db using the cursors curs
         NOTE: the ap is usually a broadcast address and will not have an id
        """
        try:
            sql = """
                   insert into probereq (fid,client,ap,ssid,sup_rates,ext_rates,vendors)
                   values (%s,%s,%s,%s,%s,%s,%s);
                  """
            ssid = None
            sup_rs = []
            ext_rs = []
            vendors = []
            for ie in l2.info_els:
                if ie[0] == mpdu.EID_SSID: ssid = ie[1]
                if ie[0] == mpdu.EID_SUPPORTED_RATES: sup_rs = ie[1]
                if ie[0] == mpdu.EID_EXTENDED_RATES: ext_rs = ie[1]
                if ie[0] == mpdu.EID_VEND_SPEC: vendors.append(ie[1][0])
            curs.execute(sql,(fid,client,ap,ssid,sup_rs,ext_rs,vendors))
        except psql.Error:
            self._err = ('probereq',fid)
            raise
        except (ValueError,IndexError,AttributeError):
            self._err = ('probereq',fid)
            raise

    def _insertproberesp(self,fid,ap,client,l2,curs):
        """
         inserts the proberesp from the ap with id ap to the sta with id client,
         seen in frame fid with details in l2 in the db using the cursors curs
        """
        try:
            sql = """
                   insert into proberesp (fid,client,ap,beacon_ts,beacon_int,
                                          ess,ibss,cf_pollable,cf_poll_req,privacy,
                                          short_pre,pbcc,ch_agility,spec_mgmt,qos,
                                          short_slot,apsd,rdo_meas,dsss_ofdm,del_ba,
                                          imm_ba,ssid,sup_rates,ext_rates,vendors)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
                  """
            beaconts = hex(l2.fixed_params['timestamp'])[2:] # store the hex (minus '0x')
            ssid = None
            sup_rs = []
            ext_rs = []
            vendors = []
            for ie in l2.info_els:
                if ie[0] == mpdu.EID_SSID: ssid = ie[1]
                if ie[0] == mpdu.EID_SUPPORTED_RATES: sup_rs = ie[1]
                if ie[0] == mpdu.EID_EXTENDED_RATES: ext_rs = ie[1]
                if ie[0] == mpdu.EID_VEND_SPEC: vendors.append(ie[1][0])
            curs.execute(sql,(fid,client,ap,beaconts,
                              l2.fixed_params['beacon-int'],
                              l2.fixed_params['capability']['ess'],
                              l2.fixed_params['capability']['ibss'],
                              l2.fixed_params['capability']['cfpollable'],
                              l2.fixed_params['capability']['cf-poll-req'],
                              l2.fixed_params['capability']['privacy'],
                              l2.fixed_params['capability']['short-pre'],
                              l2.fixed_params['capability']['pbcc'],
                              l2.fixed_params['capability']['ch-agility'],
                              l2.fixed_params['capability']['spec-mgmt'],
                              l2.fixed_params['capability']['qos'],
                              l2.fixed_params['capability']['time-slot'],
                              l2.fixed_params['capability']['apsd'],
                              l2.fixed_params['capability']['rdo-meas'],
                              l2.fixed_params['capability']['dsss-ofdm'],
                              l2.fixed_params['capability']['delayed-ba'],
                              l2.fixed_params['capability']['immediate-ba'],
                              ssid,sup_rs,ext_rs,vendors))
        except psql.Error:
            self._err = ('proberesp',fid)
            raise
        except (ValueError,IndexError,AttributeError):
            self._err = ('proberesp',fid)
            raise

    def _insertbeacon(self,fid,ap,l2,curs):
        """
         inserts the beacon from the ap with id ap, frame id fid and details in
         l2 in the db using the cursors curs
        """
        try:
            sql = """
                   insert into beacon (fid,ap,beacon_ts,beacon_int,ess,ibss,
                                       cf_pollable,cf_poll_req,privacy,short_pre,
                                       pbcc,ch_agility,spec_mgmt,qos,short_slot,
                                       apsd,rdo_meas,dsss_ofdm,del_ba,imm_ba,
                                       ssid,sup_rates,ext_rates,vendors)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
                  """
            beaconts = hex(l2.fixed_params['timestamp'])[2:] # store the hex (minus '0x')
            ssid = None
            sup_rs = []
            ext_rs = []
            vendors = []
            for ie in l2.info_els:
                if ie[0] == mpdu.EID_SSID: ssid = ie[1]
                if ie[0] == mpdu.EID_SUPPORTED_RATES: sup_rs = ie[1]
                if ie[0] == mpdu.EID_EXTENDED_RATES: ext_rs = ie[1]
                if ie[0] == mpdu.EID_VEND_SPEC: vendors.append(ie[1][0])
            curs.execute(sql,(fid,ap,beaconts,
                              l2.fixed_params['beacon-int'],
                              l2.fixed_params['capability']['ess'],
                              l2.fixed_params['capability']['ibss'],
                              l2.fixed_params['capability']['cfpollable'],
                              l2.fixed_params['capability']['cf-poll-req'],
                              l2.fixed_params['capability']['privacy'],
                              l2.fixed_params['capability']['short-pre'],
                              l2.fixed_params['capability']['pbcc'],
                              l2.fixed_params['capability']['ch-agility'],
                              l2.fixed_params['capability']['spec-mgmt'],
                              l2.fixed_params['capability']['qos'],
                              l2.fixed_params['capability']['time-slot'],
                              l2.fixed_params['capability']['apsd'],
                              l2.fixed_params['capability']['rdo-meas'],
                              l2.fixed_params['capability']['dsss-ofdm'],
                              l2.fixed_params['capability']['delayed-ba'],
                              l2.fixed_params['capability']['immediate-ba'],
                              ssid,sup_rs,ext_rs,vendors))
        except psql.Error:
            self._err = ('beacon',fid)
            raise
        except (ValueError,IndexError,AttributeError):
            self._err = ('beacon',fid)
            raise

    def _insertdisassaoc(self,fid,rx,tx,l2,curs):
        """
         inserts the disassociation frame (fid) from transmitter tx to receiver
         rx and details in l2 in the db using the cursors curs
        """
        try:
            # have to determine if this is coming from ap or from client and
            # set ap, client appropriately
            if l2.addr3 == l2.addr2:
                fromap = 1
                client = rx
                ap = tx
            else:
                fromap = 0
                client = tx
                ap = rx
            sql = """
                   insert into disassoc (fid,client,ap,fromap,reason)
                   values (%s,%s,%s,%s,%s);
                  """
            curs.execute(sql,(fid,client,ap,fromap,l2.fixed_params['reason-code']))
        except psql.Error:
            self._err = ('disassoc',fid)
            raise
        except (ValueError,IndexError,AttributeError):
            self._err = ('disassoc',fid)
            raise

    def _insertdeauth(self,fid,rx,tx,l2,curs):
        """
         inserts the deauthentication frame (fid) from transmitter tx to receiver
         rx and details in l2 in the db using the cursors curs
        """
        try:
            # have to determine if this is coming from ap or from client and
            # set ap, client appropriately
            if l2.addr3 == l2.addr2:
                fromap = 1
                client = rx
                ap = tx
            else:
                fromap = 0
                client = tx
                ap = rx
            sql = """
                   insert into deauth (fid,client,ap,fromap,reason)
                   values (%s,%s,%s,%s,%s);
                  """
            curs.execute(sql,(fid,client,ap,fromap,l2.fixed_params['reason-code']))
        except psql.Error:
            self._err = ('deauth',fid)
            raise
        except (ValueError,IndexError,AttributeError):
            self._err = ('deauth',fid)
            raise

    def _insertauth(self,fid,rx,tx,l2,curs):
        """
         inserts the deauthentication frame (fid) from transmitter tx to receiver
         rx and details in l2 in the db using the cursors curs
        """
        try:
            # have to determine if this is coming from ap or from client and
            # set ap, client appropriately
            if l2.addr3 == l2.addr2:
                fromap = 1
                client = rx
                ap = tx
            else:
                fromap = 0
                client = tx
                ap = rx
            sql = """
                   insert into auth (fid,client,ap,fromap,auth_alg,auth_trans,status)
                   values (%s,%s,%s,%s,%s,%s,%s);
                  """
            curs.execute(sql,(fid,client,ap,fromap,l2.fixed_params['algorithm-no'],
                                                   l2.fixed_params['auth-seq'],
                                                   l2.fixed_params['status-code']))
        except psql.Error:
            self._err = ('auth',fid)
            raise
        except (ValueError,IndexError,AttributeError):
            self._err = ('auth',fid)
            raise

    def _insertaction(self,fid,rx,tx,ap,noack,l2,curs):
        """
         inserts the action frame fid in bssid ap from tx to rx with details in
         l2 into the db using curs. noack is True if action no ack frame
        """
        try:
            # determine if this is from ap, to ap or intra sta
            #if l2.addr3 == l2.addr2: fromap = 1
            #elif l2.addr3 == l2.addr1: fromap = 0
            #else: fromap = 2
            if tx == ap: fromap = 1
            elif rx == ap: fromap = 0
            else: fromap = 2
            noack = int(noack)
            sql = """
                   insert into action (fid,rx,tx,ap,fromap,noack,
                                       category,action,has_el)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s);
                  """
            curs.execute(sql,(fid,rx,tx,ap,fromap,noack,
                              l2.fixed_params['category'],
                              l2.fixed_params['action'],
                              int('action-els' in l2.present)))
        except psql.Error:
            self._err = ('auth',fid)
            raise
        except (ValueError,IndexError,AttributeError):
            self._err = ('auth',fid)
            raise