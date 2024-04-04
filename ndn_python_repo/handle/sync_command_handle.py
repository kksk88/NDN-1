import asyncio as aio
import logging
from ndn.app import NDNApp
from ndn.encoding import Name, NonStrictName, DecodeError, Component, parse_data
from ndn.types import InterestNack, InterestTimeout
from . import ReadHandle, CommandHandle
from ..command import RepoCommandRes, RepoCommandParam, SyncParam, SyncStatus, RepoStatCode
from ..utils import concurrent_fetcher, PubSub, PassiveSvs, IdNamingConv
from ..storage import Storage
from typing import Optional, Tuple, List, Dict
from hashlib import sha256
from .utils import normalize_block_ids


class SyncCommandHandle(CommandHandle):
    """
    SyncCommandHandle processes insert command interests, and fetches corresponding data to
    store them into the database.
    TODO: Add validator
    """
    def __init__(self, app: NDNApp, storage: Storage, pb: PubSub, read_handle: ReadHandle,
                 config: dict):
        """
        Sync handle need to keep a reference to sync handle to register new prefixes.

        :param app: NDNApp.
        :param storage: Storage.
        :param read_handle: ReadHandle. This param is necessary, because WriteCommandHandle need to
            call ReadHandle.listen() to register new prefixes.
        """
        super(SyncCommandHandle, self).__init__(app, storage, pb, config)
        self.m_read_handle = read_handle
        self.prefix = None
        self.register_root = config['repo_config']['register_root']
        # sync specific states
        self.states_on_disk = {}

    async def listen(self, prefix: NonStrictName):
        """
        Register routes for command interests.
        This function needs to be called explicitly after initialization.

        :param prefix: NonStrictName. The name prefix to listen on.
        """
        self.prefix = Name.normalize(prefix)

        # subscribe to sync messages
        self.pb.subscribe(self.prefix + Name.from_str('sync'), self._on_sync_msg)

    def recover_from_states(self, states: Dict):
        self.states_on_disk = states
        # recover sync
        for sync_group, group_states in self.states_on_disk.items():
            new_svs = PassiveSvs(sync_group, lambda svs: aio.create_task(self.fetch_missing_data(svs)))
            new_svs.decode_from_states(group_states['svs_client_states'])
            logging.info(f'Recover sync for {Name.to_str(sync_group)}')
            group_fetched_dict = group_states['fetched_dict']
            logging.info(f'Sync progress: {group_fetched_dict}')
            new_svs.start(self.app)

    def _on_sync_msg(self, msg):
        try:
            cmd_param = RepoCommandParam.parse(msg)
            request_no = sha256(bytes(msg)).digest()
            if not cmd_param.sync_groups:
                raise DecodeError('Missing sync groups')
            for group in cmd_param.sync_groups:
                if not group.sync_prefix:
                    raise DecodeError('Missing name for one or more sync groups')
        except (DecodeError, IndexError) as exc:
            logging.warning(f'Parameter interest blob decoding failed w/ exception: {exc}')
            return
        aio.create_task(self._process_sync(cmd_param, request_no))

    async def _process_sync(self, cmd_param: RepoCommandParam, request_no: bytes):
        """
        Process sync command.
        Return to client with status code 100 immediately, and then start sync process.
        """
        groups = cmd_param.sync_groups
        logging.info(f'Recved sync command: {request_no.hex()}')

        # Cached status response
        # Note: no coroutine switching here, so no multithread conflicts
        def _init_sync_stat(param: SyncParam) -> SyncStatus:
            ret = SyncStatus()
            ret.name = param.sync_prefix
            ret.status_code = RepoStatCode.ROGER
            ret.insert_num = 0
            return ret

        # Note: stat is hold by reference
        stat = RepoCommandRes()
        stat.status_code = RepoStatCode.IN_PROGRESS
        stat.sync_groups = [_init_sync_stat(group) for group in groups]
        self.m_processes[request_no] = stat
        # start sync
        for idx, group in enumerate(groups):
            # check duplicate
            if Name.to_str(group.sync_prefix) in self.states_on_disk:
                logging.info(f'duplicate sync for : {Name.to_str(group.sync_prefix)}')
                continue
            new_svs = PassiveSvs(group.sync_prefix, lambda svs: aio.create_task(self.fetch_missing_data(svs)))
            new_svs.start(self.app)
            # write states
            self.states_on_disk[Name.to_str(group.sync_prefix)] = {}
            new_states = self.states_on_disk[Name.to_str(group.sync_prefix)]
            new_states['fetched_dict'] = {}
            new_states['svs_client_states'] = {}
            new_states['data_name_dedupe'] = group.data_name_dedupe
            new_states['check_status'] = {}
            # Remember the prefixes to register
            if group.register_prefix:
                is_existing = CommandHandle.add_registered_prefix_in_storage(self.storage, group.register_prefix)
                # If repo does not register root prefix, the client tells repo what to register
                if not self.register_root and not is_existing:
                    self.m_read_handle.listen(group.register_prefix)
            CommandHandle.add_sync_group_in_storage(self.storage, group.sync_prefix)
            new_states['svs_client_states'] = new_svs.encode_into_states()
            CommandHandle.add_sync_states_in_storage(self.storage, group.sync_prefix, new_states)
            
    async def fetch_missing_data(self, svs: PassiveSvs):
        local_sv = svs.local_sv.copy()
        for node_id, seq in local_sv.items():
            group_states = self.states_on_disk[Name.to_str(svs.base_prefix)]
            group_fetched_dict = group_states['fetched_dict']
            group_data_name_dedupe = group_states['data_name_dedupe']
            fetched_seq = group_fetched_dict.get(node_id, 0)
            node_name = Name.from_str(node_id) + svs.base_prefix
            if group_data_name_dedupe:
                data_prefix = [i for n, i in enumerate(node_name) if i not in node_name[:n]]
            else:
                data_prefix = node_name
            # I do not treat fetching failure as hard failure
            if fetched_seq < seq:
                async for (data_name, _, data_content, data_bytes) in (
                    concurrent_fetcher(self.app, data_prefix,
                                        start_id=fetched_seq+1, end_id=seq,
                                        semaphore=aio.Semaphore(10),
                                        name_conv = IdNamingConv.SEQUENCE,
                                        max_retries = -1)):
                    # put into storage asap
                    self.storage.put_data_packet(data_name, data_bytes)
                    # not very sure the side effect
                    group_fetched_dict[node_id] = Component.to_number(data_name[-1])
                    logging.info(f'Sync progress: {group_fetched_dict}')
                    group_states['svs_client_states'] = svs.encode_into_states()
                    CommandHandle.add_sync_states_in_storage(self.storage, svs.base_prefix, group_states)
                    '''
                    Python-repo specific logic: if the data content contains a data name,
                    assuming the data object pointed by is a segmented, and fetching all
                    data segments related to this object name
                    '''
                    try:
                        _, _, inner_data_content, _ = parse_data(data_content)
                        obj_pointer = Name.from_bytes(inner_data_content)
                    except (TypeError, IndexError, ValueError):
                        logging.debug(f'Data does not include an object pointer, skip')
                        continue
                    logging.info(f'Discovered a pointer, fetching data segments for {Name.to_str(obj_pointer)}')
                    async for (data_name, _, _, data_bytes) in (
                        concurrent_fetcher(self.app, obj_pointer,
                            start_id=0, end_id=None, semaphore=aio.Semaphore(10))):
                        self.storage.put_data_packet(data_name, data_bytes)
    # async def fetch_single_data(self, pkt_name: NonStrictName,
    #                             forwarding_hint: Optional[List[NonStrictName]] = None):
    #     """
    #     Fetch one Data packet.
    #     :param name: NonStrictName.
    #     :return:  Number of data packets fetched.
    #     """
    #     trial_times = 0
    #     while True:
    #         trial_times += 1
    #         if trial_times > 30:
    #             logging.info(f'Interest {Name.to_str(pkt_name)} running out of retries')
    #             return None, None
    #         try:
    #             _, _, data_content, data_bytes = await self.app.express_interest(
    #                 pkt_name, need_raw_packet=True, can_be_prefix=False, lifetime=500,
    #                 forwarding_hint=forwarding_hint)
    #             return data_content, data_bytes
    #         except InterestNack as e:
    #             logging.info(f'Interest {Name.to_str(pkt_name)} Nacked with reason={e.reason}')
    #             return None, None
    #         except InterestTimeout:
    #             pass