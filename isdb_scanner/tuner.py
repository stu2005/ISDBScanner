
from __future__ import annotations

import libusb_package
import re
import signal
import subprocess
import sys
import threading
import time
import usb.core
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast, Literal

from isdb_scanner.constants import (
    DVBDeviceInfo,
    DVB_INTERFACE_TUNER_DEVICE_PATHS,
    ISDBT_TUNER_DEVICE_PATHS,
    ISDBS_TUNER_DEVICE_PATHS,
    ISDB_MULTI_TUNER_DEVICE_PATHS,
)


class ISDBTuner:
    """ ISDB-T/ISDB-S チューナーデバイスを操作するクラス (recisdb のラッパー) """


    def __init__(self, device_path: Path, output_recisdb_log: bool = False) -> None:
        """
        ISDBTuner を初期化する

        Args:
            device_path (Path): デバイスファイルのパス
            output_recisdb_log (bool, optional): recisdb のログを出力するかどうか. Defaults to False.
        """

        # 操作対象のデバイスファイルは最低でもキャラクタデバイスである必要がある
        # チューナードライバが chardev 版か DVB 版かに関わらず、デバイスファイルはキャラクタデバイスになる
        assert device_path.exists() and device_path.is_char_device(), f'Invalid tuner device: {device_path}'
        self.output_recisdb_log = output_recisdb_log

        # 指定されたデバイスファイルに紐づくチューナーデバイスの情報を取得
        self.device_path = device_path
        self.device_type: Literal['Chardev', 'V4L-DVB']
        self.type: Literal['ISDB-T', 'ISDB-S', 'ISDB-T/ISDB-S']
        self.name: str
        self.device_type, self.type, self.name = self.__getTunerDeviceInfo()

        # 前回チューナーオープンが失敗した (TunerOpeningError が発生した) かどうか
        self.last_tuner_opening_failed = False


    def __getTunerDeviceInfo(self) -> tuple[Literal['Chardev', 'V4L-DVB'], Literal['ISDB-T', 'ISDB-S', 'ISDB-T/ISDB-S'], str]:
        """
        チューナーデバイスの種類と名前を取得する

        Returns:
            tuple[Literal['Chardev', 'V4L-DVB'], Literal['ISDB-T', 'ISDB-S', 'ISDB-T/ISDB-S'], str]: チューナーデバイスの種類と名前
        """

        # /dev/pt1videoX・/dev/pt3videoX・/dev/px4videoX の X の部分を取得して、チューナーの種類と番号を返す共通処理
        def GetPT1PT3PX4VideoDeviceInfo() -> tuple[Literal['ISDB-T', 'ISDB-S'], int]:

            # デバイスパスから数字部分を抽出
            if str(self.device_path).startswith('/dev/pt1video'):
                device_number = int(str(self.device_path).split('pt1video')[-1])
            elif str(self.device_path).startswith('/dev/pt3video'):
                device_number = int(str(self.device_path).split('pt3video')[-1])
            elif str(self.device_path).startswith('/dev/px4video'):
                device_number = int(str(self.device_path).split('px4video')[-1])
            else:
                assert False, f'Unknown tuner device: {self.device_path}'

            # デバイスタイプとインデックスを自動判定
            # ISDB-T: 2,3,6,7,10,11,14,15 ... (2個おき)
            # ISDB-S: 0,1,4,5,8,9,12,13 ... (2個おき)
            remainder = device_number % 4
            if remainder in [0, 1]:
                tuner_type = 'ISDB-S'
                tuner_number = device_number // 4 * 2 + 1
            elif remainder in [2, 3]:
                tuner_type = 'ISDB-T'
                tuner_number = (device_number - 2) // 4 * 2 + 1
            else:
                assert False, f'Unknown tuner device: {self.device_path}'
            if remainder in [1, 3]:
                tuner_number += 1

            return tuner_type, tuner_number

        # V4L-DVB 版ドライバのチューナーデバイス
        if str(self.device_path).startswith('/dev/dvb'):
            # システムで利用可能な DVB デバイスの中から、デバイスパスが一致するデバイス情報を探す
            for device_info in ISDBTuner.getAvailableDVBDeviceInfos():
                if device_info.device_path == self.device_path:
                    return ('V4L-DVB', device_info.tuner_type, device_info.tuner_name)

        # ***** ここからは chardev 版ドライバのチューナーデバイス *****

        # Earthsoft PT1/PT2
        if str(self.device_path).startswith('/dev/pt1video'):
            tuner_type, tuner_number = GetPT1PT3PX4VideoDeviceInfo()
            return ('Chardev', tuner_type, f'Earthsoft PT1/PT2 ({tuner_type}) #{tuner_number}')

        # Earthsoft PT3
        if str(self.device_path).startswith('/dev/pt3video'):
            tuner_type, tuner_number = GetPT1PT3PX4VideoDeviceInfo()
            return ('Chardev', tuner_type, f'Earthsoft PT3 ({tuner_type}) #{tuner_number}')

        # PLEX PX-W3U4/PX-Q3U4/PX-W3PE4/PX-Q3PE4/PX-W3PE5/PX-Q3PE5 (PX4/PX5 Series)
        if str(self.device_path).startswith('/dev/px4video'):
            tuner_type, tuner_number = GetPT1PT3PX4VideoDeviceInfo()

            # PX4/PX5 チューナーの製造元の Digibest の Vendor ID
            VENDOR_ID = 0x0511

            # PX4/PX5 チューナーの Product ID とチューナー名の対応表
            # ref: https://github.com/tsukumijima/px4_drv/blob/develop/driver/px4_usb.h
            PRODUCT_ID_TO_TUNER_NAME = {
                0x083f: 'PX-W3U4',
                0x084a: 'PX-Q3U4',
                0x023f: 'PX-W3PE4',
                0x024a: 'PX-Q3PE4',
                0x073f: 'PX-W3PE5',
                0x074a: 'PX-Q3PE5',
            }

            # 接続されている USB デバイスの中から PX4/PX5 チューナーを探す
            backend = libusb_package.get_libusb1_backend()
            devices = usb.core.find(find_all=True, idVendor=VENDOR_ID, backend=backend)
            assert devices is not None, 'Failed to find USB devices.'
            tuner_names: list[str] = []
            for device in devices:
                if hasattr(device, 'idProduct') is False:  # 念のため
                    continue
                product_id = cast(Any, device).idProduct
                if product_id in PRODUCT_ID_TO_TUNER_NAME:
                    tuner_names.append(PRODUCT_ID_TO_TUNER_NAME[product_id])

            # 重複するチューナー名を削除し、チューナー名を連結
            ## 同一機種を複数接続している場合はチューナー名が重複するため、重複を削除
            tuner_name = '/'.join(list(set(tuner_names)))

            return ('Chardev', tuner_type, f'PLEX {tuner_name} ({tuner_type}) #{tuner_number}')

        # PLEX PX-S1UR
        if str(self.device_path).startswith('/dev/pxs1urvideo'):
            return ('Chardev', 'ISDB-T', f'PLEX PX-S1UR #{int(str(self.device_path).split("pxs1urvideo")[-1]) + 1}')

        # PLEX PX-M1UR
        if str(self.device_path).startswith('/dev/pxm1urvideo'):
            return ('Chardev', 'ISDB-T/ISDB-S', f'PLEX PX-M1UR #{int(str(self.device_path).split("pxm1urvideo")[-1]) + 1}')

        # PLEX PX-MLT5PE
        if str(self.device_path).startswith('/dev/pxmlt5video'):
            return ('Chardev', 'ISDB-T/ISDB-S', f'PLEX PX-MLT5PE #{int(str(self.device_path).split("pxmlt5video")[-1]) + 1}')

        # PLEX PX-MLT8PE
        if str(self.device_path).startswith('/dev/pxmlt8video'):
            return ('Chardev', 'ISDB-T/ISDB-S', f'PLEX PX-MLT8PE #{int(str(self.device_path).split("pxmlt8video")[-1]) + 1}')

        # e-better DTV02A-4TS-P
        if str(self.device_path).startswith('/dev/isdb6014video'):
            return ('Chardev', 'ISDB-T/ISDB-S', f'e-better DTV02A-4TS-P #{int(str(self.device_path).split("isdb6014video")[-1]) + 1}')

        # e-better DTV02A-1T1S-U
        if str(self.device_path).startswith('/dev/isdb2056video'):
            return ('Chardev', 'ISDB-T/ISDB-S', f'e-better DTV02A-1T1S-U #{int(str(self.device_path).split("isdb2056video")[-1]) + 1}')

        # ここには到達しないはず
        assert False, f'Unknown tuner device: {self.device_path}'


    def tune(self, physical_channel_recisdb: str, recording_time: float = 10.0, tune_timeout: float = 7.0) -> bytearray:
        """
        チューナーデバイスから指定された物理チャンネルを受信し、選局/受信できなかった場合は例外を送出する
        録画時間にはチューナーオープンに掛かった時間を含まない
        選局タイムアウト発生時、チューナーのクローズに時間がかかる関係で最小でも合計 7 秒程度の時間が掛かる

        Args:
            physical_channel_recisdb (str): recisdb が受け付けるフォーマットの物理チャンネル (ex: "T13", "BS23_3", "CS04")
            recording_time (float, optional): 録画時間 (秒). Defaults to 10.0.
            tune_timeout (float, optional): 選局 (チューナーオープン) のタイムアウト時間 (秒). Defaults to 7.0.

        Returns:
            bytearray: 受信したデータ

        Raises:
            TunerOpeningError: チューナーをオープンできなかった場合
            TunerTuningError: チャンネルを選局できなかった場合
            TunerOutputError: 受信したデータが小さすぎる場合
        """

        self.last_tuner_opening_failed = False

        # recisdb (チューナープロセス) を起動
        process = subprocess.Popen(
            ['recisdb', 'tune', '--device', str(self.device_path), '--channel', physical_channel_recisdb, '--time', str(recording_time), '-'],
            stdout = subprocess.PIPE,
            stderr = subprocess.PIPE,
        )

        # それぞれ別スレッドで標準出力と標準エラー出力の読み込みを開始
        stdout: bytearray = bytearray()
        is_stdout_arrived = False
        def stdout_thread_func():
            nonlocal stdout, is_stdout_arrived
            assert process.stdout is not None
            while True:
                data = process.stdout.read(188)
                is_stdout_arrived = True
                if len(data) == 0:
                    break
                stdout.extend(data)
        stderr: bytes = b''
        def stderr_thread_func():
            nonlocal stderr
            assert process.stderr is not None
            while True:
                data = process.stderr.read(1)
                if len(data) == 0:
                    break
                stderr += data
                if self.output_recisdb_log is True:
                    sys.stderr.buffer.write(data)
                    sys.stderr.buffer.flush()
        stdout_thread = threading.Thread(target=stdout_thread_func)
        stderr_thread = threading.Thread(target=stderr_thread_func)
        stdout_thread.start()
        stderr_thread.start()

        # プロセスが終了するか、選局 (チューナーオープン) のタイムアウト秒数に達するまで待機
        # 標準出力から TS ストリームが出力されるようになったらタイムアウト秒数のカウントを停止
        tune_timeout_count = 0
        while process.poll() is None and tune_timeout_count < tune_timeout:
            time.sleep(0.01)
            if is_stdout_arrived is False:
                tune_timeout_count += 0.01

        # この時点でプロセスが終了しておらず、標準出力からまだ TS ストリームを受け取っていない場合
        # プロセスを終了 (Ctrl+C を送信) し、タイムアウトエラーを送出する
        if process.poll() is None and is_stdout_arrived is False:
            process.send_signal(signal.SIGINT)
            # ここでプロセスが完全に終了するまで待機しないと、続けて別のチャンネルを選局する際にデバイス使用中エラーが発生してしまう
            process.wait()
            raise TunerTuningError('Channel selection timed out.')

        # プロセスと標準エラー出力スレッドの終了を待機
        process.wait()
        stderr_thread.join()

        # この時点でリターンコードが 0 でなければ選局または受信に失敗している
        if process.returncode != 0:

            # エラメッセージを正規表現で取得
            result = re.search(r'ERROR:\s+(.+)', stderr.decode('utf-8'))
            if result is not None:
                error_message = result.group(1)
            else:
                error_message = 'Channel selection failed due to an unknown error.'

            # チューナーオープン時のエラー
            if error_message in [
                'The tuner device does not exist.',
                'The tuner device is already in use.',
                'The tuner device is busy.',
                'The tuner device does not support the ioctl system call.',
            ] or error_message.startswith('Cannot open the device.'):
                self.last_tuner_opening_failed = True
                raise TunerOpeningError(error_message)

            # それ以外は選局/受信時のエラーと判断
            raise TunerTuningError(error_message)

        # 受信していれば（チューナーオープン時間を含めても）100KB 以上のデータが得られるはず
        # それ未満の場合は選局に失敗している
        if len(stdout) < 100 * 1024:
            raise TunerOutputError('The tuner output is too small.')

        # 受信したデータを返す
        return stdout


    def getSignalLevel(self, physical_channel_recisdb: str) -> tuple[subprocess.Popen[bytes], Iterator[float]]:
        """
        チューナーデバイスから指定された物理チャンネルを受信し、イテレータで信号レベルを返す
        この関数はイテレータを呼び終わってもプロセスを終了しないので、呼び出し側で明示的にプロセスを終了する必要がある

        Args:
            physical_channel_recisdb (str): recisdb が受け付けるフォーマットの物理チャンネル (ex: "T13", "BS23_3", "CS04")

        Returns:
            tuple[subprocess.Popen, Iterator[float]]: チューナープロセスと信号レベルを返すイテレータ
        """

        # recisdb (チューナープロセス) を起動
        process = subprocess.Popen(
            ['recisdb', 'checksignal', '--device', str(self.device_path), '--channel', physical_channel_recisdb],
            stdout = subprocess.PIPE,
            stderr = None if self.output_recisdb_log is True else subprocess.DEVNULL,
        )

        # 標準出力に一行ずつ受信感度が "30.00dB" のように出力されるので、随時パースしてイテレータで返す
        ## 選局/受信に失敗したか、あるいはユーザーが手動でプロセスを終了させた場合は StopIteration が発生する
        def iterator() -> Iterator[float]:
            assert process.stdout is not None
            while True:

                # \r が出力されるまで 1 バイトずつ読み込む
                line = b''
                while True:
                    char = process.stdout.read(1)
                    if char == b'\r' or char == b'':
                        break
                    line += char

                # プロセスが終了していたら終了
                if process.poll() is not None:
                    process.send_signal(signal.SIGINT)
                    process.wait()
                    raise StopIteration

                # 信号レベルをパースして随時返す
                result = re.search(r'(\d+\.\d+)dB', line.decode('utf-8').strip())
                if result is None:
                    continue
                yield float(result.group(1))

        return process, iterator()


    def getSignalLevelMean(self, physical_channel_recisdb: str) -> float | None:
        """
        チューナーデバイスから指定された物理チャンネルを受信し、5回の平均信号レベルを返す

        Args:
            physical_channel_recisdb (str): recisdb が受け付けるフォーマットの物理チャンネル (ex: "T13", "BS23_3", "CS04")

        Returns:
            float | None: 平均信号レベル (選局失敗時は None)
        """

        # 信号レベルを取得するイテレータを取得
        process, iterator = self.getSignalLevel(physical_channel_recisdb)

        # 5回分の信号レベルを取得
        # もし信号レベルの取得中にプロセスが終了した場合は選局に失敗しているので None を返す
        signal_levels: list[float] = []
        for _ in range(5):
            try:
                signal_levels.append(next(iterator))
            except RuntimeError:
                return None

        # プロセスを終了
        process.send_signal(signal.SIGINT)
        process.wait()

        # 平均信号レベルを返す
        return sum(signal_levels) / len(signal_levels)


    @staticmethod
    def getAvailableDVBDeviceInfos() -> list[DVBDeviceInfo]:
        """
        システムで利用可能な DVB デバイスの情報を取得する

        Returns:
            list[DVBDeviceInfo]: システムで利用可能な DVB デバイスの情報
        """

        device_infos: list[DVBDeviceInfo] = []
        for device_path in DVB_INTERFACE_TUNER_DEVICE_PATHS:

            # /dev/dvb/adapter0/frontend0 のようなパスから、DVB デバイス番号 (adapterX) を取得
            search = re.search(r'\/dev\/dvb\/adapter(\d+)\/frontend0', device_path)
            if search is None:
                continue
            adapter_number = int(search.group(1))

            # USB デバイス
            if Path(f'/sys/class/dvb/dvb{adapter_number}.frontend0/device/idVendor').exists() and \
            Path(f'/sys/class/dvb/dvb{adapter_number}.frontend0/device/idProduct').exists():
                with open(f'/sys/class/dvb/dvb{adapter_number}.frontend0/device/idVendor') as f:
                    vendor_id = int(f.read().strip(), 16)
                with open(f'/sys/class/dvb/dvb{adapter_number}.frontend0/device/idProduct') as f:
                    product_id = int(f.read().strip(), 16)

                # USB ID の参考資料
                # ref: https://github.com/torvalds/linux/blob/v6.5/drivers/media/usb/siano/smsusb.c#L622-L711

                # MyGica S270 (旧ロット?)
                ## 数年前まで VASTDTV のパッケージになる前に売られていた製品と思われる
                if vendor_id == 0x187f and product_id == 0x0600:
                    device_infos.append(DVBDeviceInfo(
                        device_path = Path(device_path),
                        tuner_type = 'ISDB-T',
                        tuner_name = 'MyGica S270',
                    ))

                # PLEX PX-S1UD / VASTDTV VT20
                ## PX-S1UD と、VASTDTV VT20 として売られているチューナーは USB ID 含めパッケージ以外は同一の製品
                ## VASTDTV VT20 が MyGica S270 として販売されている場合もあって謎…… (おそらく MyGica も VASTDTV も Geniatech のブランド名)
                if vendor_id == 0x3275 and product_id == 0x0080:
                    device_infos.append(DVBDeviceInfo(
                        device_path = Path(device_path),
                        tuner_type = 'ISDB-T',
                        tuner_name = 'PLEX PX-S1UD / VASTDTV VT20',
                    ))

            # PCI デバイス
            elif Path(f'/sys/class/dvb/dvb{adapter_number}.frontend0/device/vendor').exists() and \
                 Path(f'/sys/class/dvb/dvb{adapter_number}.frontend0/device/device').exists() and \
                 Path(f'/sys/class/dvb/dvb{adapter_number}.frontend0/device/subsystem_vendor').exists() and \
                 Path(f'/sys/class/dvb/dvb{adapter_number}.frontend0/device/subsystem_device').exists():
                with open(f'/sys/class/dvb/dvb{adapter_number}.frontend0/device/vendor') as f:
                    vendor_id = int(f.read().strip(), 16)
                with open(f'/sys/class/dvb/dvb{adapter_number}.frontend0/device/device') as f:
                    device_id = int(f.read().strip(), 16)
                with open(f'/sys/class/dvb/dvb{adapter_number}.frontend0/device/subsystem_vendor') as f:
                    subsystem_vendor_id = int(f.read().strip(), 16)
                with open(f'/sys/class/dvb/dvb{adapter_number}.frontend0/device/subsystem_device') as f:
                    subsystem_device_id = int(f.read().strip(), 16)

                # PCI ID の参考資料
                # PT1/PT2/PT3 の PCI vendor_id は FPGA 回路のメーカーのものが使われているみたい
                # ref: https://cateee.net/lkddb/web-lkddb/DVB_PT1.html
                # ref: https://cateee.net/lkddb/web-lkddb/DVB_PT3.html
                # ref: https://github.com/DigitalDevices/dddvb/blob/master/ddbridge/ddbridge-hw.c#L861-L929

                # Earthsoft PT1
                if vendor_id == 0x10ee and device_id == 0x211a:
                    device_infos.append(DVBDeviceInfo(
                        device_path = Path(device_path),
                        tuner_type = 'ISDB-T',  # TODO!!!!
                        tuner_name = 'Earthsoft PT1',
                    ))

                # Earthsoft PT2
                if vendor_id == 0x10ee and device_id == 0x222a:
                    device_infos.append(DVBDeviceInfo(
                        device_path = Path(device_path),
                        tuner_type = 'ISDB-T',  # TODO!!!!
                        tuner_name = 'Earthsoft PT2',
                    ))

                # Earthsoft PT3
                if vendor_id == 0x1172 and device_id == 0x4c15 and subsystem_vendor_id == 0xee8d and subsystem_device_id == 0x0368:
                    device_infos.append(DVBDeviceInfo(
                        device_path = Path(device_path),
                        tuner_type = 'ISDB-T',  # TODO!!!!
                        tuner_name = 'Earthsoft PT3',
                    ))

                # Digital Devices
                if vendor_id == 0xdd01:

                    # DD Max M4
                    ## ISDB-T/ISDB-S 以外の DVB などの放送方式も受信できるが、ISDBScanner では ISDB-T/ISDB-S 以外をサポートしないため、ISDB-T/ISDB-S 共用チューナーとして扱う
                    if device_id == 0x000a and subsystem_device_id == 0x0050:
                        device_infos.append(DVBDeviceInfo(
                            device_path = Path(device_path),
                            tuner_type = 'ISDB-T/ISDB-S',
                            tuner_name = 'Digital Devices DD Max M4',
                        ))

                    # DD Max M8 (未発売)
                    ## ISDB-T/ISDB-S 以外の DVB などの放送方式も受信できるが、ISDBScanner では ISDB-T/ISDB-S 以外をサポートしないため、ISDB-T/ISDB-S 共用チューナーとして扱う
                    if device_id == 0x0022 and subsystem_device_id == 0x0052:
                        device_infos.append(DVBDeviceInfo(
                            device_path = Path(device_path),
                            tuner_type = 'ISDB-T/ISDB-S',
                            tuner_name = 'Digital Devices DD Max M8',
                        ))

                    # DD Max M8A (未発売)
                    ## ISDB-T/ISDB-S 以外の DVB などの放送方式も受信できるが、ISDBScanner では ISDB-T/ISDB-S 以外をサポートしないため、ISDB-T/ISDB-S 共用チューナーとして扱う
                    if device_id == 0x0024 and subsystem_device_id == 0x0053:
                        device_infos.append(DVBDeviceInfo(
                            device_path = Path(device_path),
                            tuner_type = 'ISDB-T/ISDB-S',
                            tuner_name = 'Digital Devices DD Max M8A',
                        ))

                    # DD Max A8i (終売)
                    ## ISDB-T 以外の DVB などの放送方式も受信できるが、ISDBScanner では ISDB-T/ISDB-S 以外をサポートしないため、ISDB-T 専用チューナーとして扱う
                    if device_id == 0x0008 and subsystem_device_id == 0x0036:
                        device_infos.append(DVBDeviceInfo(
                            device_path = Path(device_path),
                            tuner_type = 'ISDB-T',
                            tuner_name = 'Digital Devices DD Max A8i',
                        ))

            # 基本到達しないはず
            else:
                continue

        # 同一チューナー名ごとにグループ化し、それぞれ DVB デバイス番号の昇順でソートし、#1, #2, ... の suffix を付ける
        device_infos_grouped: dict[str, list[DVBDeviceInfo]] = {}
        for device_info in device_infos:
            if device_info.tuner_name not in device_infos_grouped:
                device_infos_grouped[device_info.tuner_name] = []
            device_infos_grouped[device_info.tuner_name].append(device_info)
        for device_infos in device_infos_grouped.values():
            device_infos.sort(key=lambda x: x.device_path)
            for i, device_info in enumerate(device_infos):
                device_info.tuner_name += f' #{i + 1}'

        return device_infos


    @staticmethod
    def getAvailableISDBTTuners() -> list[ISDBTuner]:
        """
        利用可能な ISDB-T チューナーのリストを取得する
        ISDB-T 専用チューナーと ISDB-T/ISDB-S 共用チューナーの両方が含まれる

        Returns:
            list[ISDBTuner]: 利用可能な ISDB-T チューナーのリスト
        """

        # ISDB-T 専用チューナーと ISDB-T/ISDB-S 共用チューナーの両方を含む
        return ISDBTuner.getAvailableISDBTOnlyTuners() + ISDBTuner.getAvailableMultiTuners()


    @staticmethod
    def getAvailableISDBTOnlyTuners() -> list[ISDBTuner]:
        """
        利用可能な ISDB-T チューナーのリストを取得する
        ISDB-T 専用チューナーのみが含まれる

        Returns:
            list[ISDBTuner]: 利用可能な ISDB-T 専用チューナーのリスト
        """

        # 存在するデバイスのパスを取得し、ISDBTuner を初期化してリストに追加
        # chardev デバイスを優先し、V4L-DVB デバイスは後から追加する
        tuners: list[ISDBTuner] = []
        for device_path in ISDBT_TUNER_DEVICE_PATHS + DVB_INTERFACE_TUNER_DEVICE_PATHS:
            device_path = Path(device_path)
            # キャラクタデバイスファイルかつ ISDB-T 専用チューナーであればリストに追加
            if device_path.exists() and device_path.is_char_device():
                tuner = ISDBTuner(device_path)
                if tuner.type == 'ISDB-T':
                    tuners.append(tuner)

        return tuners


    @staticmethod
    def getAvailableISDBSTuners() -> list[ISDBTuner]:
        """
        利用可能な ISDB-S チューナーのリストを取得する
        ISDB-S 専用チューナーと ISDB-T/ISDB-S 共用チューナーの両方が含まれる

        Returns:
            list[ISDBTuner]: 利用可能な ISDB-S チューナーのリスト
        """

        # ISDB-S 専用チューナーと ISDB-T/ISDB-S 共用チューナーの両方を含む
        return ISDBTuner.getAvailableISDBSOnlyTuners() + ISDBTuner.getAvailableMultiTuners()


    @staticmethod
    def getAvailableISDBSOnlyTuners() -> list[ISDBTuner]:
        """
        利用可能な ISDB-S チューナーのリストを取得する
        ISDB-S 専用チューナーのみが含まれる

        Returns:
            list[ISDBTuner]: 利用可能な ISDB-S 専用チューナーのリスト
        """

        # 存在するデバイスのパスを取得し、ISDBTuner を初期化してリストに追加
        # chardev デバイスを優先し、V4L-DVB デバイスは後から追加する
        tuners: list[ISDBTuner] = []
        for device_path in ISDBS_TUNER_DEVICE_PATHS + DVB_INTERFACE_TUNER_DEVICE_PATHS:
            device_path = Path(device_path)
            # キャラクタデバイスファイルかつ ISDB-S 専用チューナーであればリストに追加
            if device_path.exists() and device_path.is_char_device():
                tuner = ISDBTuner(device_path)
                if tuner.type == 'ISDB-S':
                    tuners.append(tuner)

        return tuners


    @staticmethod
    def getAvailableMultiTuners() -> list[ISDBTuner]:
        """
        利用可能な ISDB-T/ISDB-S 共用チューナーのリストを取得する

        Returns:
            list[ISDBTuner]: 利用可能な ISDB-T/ISDB-S 共用チューナーのリスト
        """

        # 存在するデバイスのパスを取得し、ISDBTuner を初期化してリストに追加
        # chardev デバイスを優先し、V4L-DVB デバイスは後から追加する
        tuners: list[ISDBTuner] = []
        for device_path in ISDB_MULTI_TUNER_DEVICE_PATHS + DVB_INTERFACE_TUNER_DEVICE_PATHS:
            device_path = Path(device_path)
            # キャラクタデバイスファイルかつ ISDB-T/ISDB-S 共用チューナーであればリストに追加
            if device_path.exists() and device_path.is_char_device():
                tuner = ISDBTuner(device_path)
                if tuner.type == 'ISDB-T/ISDB-S':
                    tuners.append(tuner)

        return tuners


class TunerOpeningError(Exception):
    """ チューナーのオープンに失敗したことを表す例外 """
    pass


class TunerTuningError(Exception):
    """ チューナーの選局に失敗したことを表す例外 """
    pass


class TunerOutputError(Exception):
    """ チューナーから出力されたデータが不正なことを表す例外 """
    pass
