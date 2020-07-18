"""
smartmeter -- Send P1 telegram to an InfluxDB API.

Credits for the meter reading part (+ parsing and CRC) go to https://github.com/nrocco/smeterd
"""

import re
from argparse import ArgumentParser

import crcmod.predefined
import serial
from influxdb import InfluxDBClient

crc16 = crcmod.predefined.mkPredefinedCrcFun('crc16')


class SmartMeter:
	def __init__(self, port, *args, **kwargs):
		try:
			self.serial = serial.Serial(
				port,
				kwargs.get('baudrate', 115200),
				timeout=10,
				bytesize=serial.SEVENBITS,
				parity=serial.PARITY_EVEN,
				stopbits=serial.STOPBITS_ONE
			)
		except (serial.SerialException, OSError) as e:
			raise SmartMeterError(e)
		else:
			self.serial.setRTS(False)
			self.port = self.serial.name

	def connect(self):
		if not self.serial.isOpen():
			self.serial.open()
			self.serial.setRTS(False)

	def disconnect(self):
		if self.serial.isOpen():
			self.serial.close()

	def connected(self):
		return self.serial.isOpen()

	def read_one_packet(self):
		datagram = b''
		lines_read = 0
		startFound = False
		endFound = False
		max_lines = 35  # largest known telegram has 35 lines

		while not startFound or not endFound:
			try:
				line = self.serial.readline()
			except Exception as e:
				raise SmartMeterError(e)

			lines_read += 1

			if re.match(b'.*(?=/)', line):
				startFound = True
				endFound = False
				datagram = line.lstrip()
			elif re.match(b'(?=!)', line):
				endFound = True
				datagram = datagram + line
			else:
				datagram = datagram + line

		# TODO: build in some protection for infinite loops

		return P1Packet(datagram)


class SmartMeterError(Exception):
	pass


class P1PacketError(Exception):
	pass


class P1Packet:
	_datagram = ''

	def __init__(self, datagram):
		self._datagram = datagram

		self.validate()

		keys = {}

		keys['+T1'] = self.get_float(b'^1-0:1\.8\.1\(([0-9]+\.[0-9]+)\*kWh\)\r\n')
		keys['-T1'] = self.get_float(b'^1-0:2\.8\.1\(([0-9]+\.[0-9]+)\*kWh\)\r\n')

		keys['+T2'] = self.get_float(b'^1-0:1\.8\.2\(([0-9]+\.[0-9]+)\*kWh\)\r\n')
		keys['-T2'] = self.get_float(b'^1-0:2\.8\.2\(([0-9]+\.[0-9]+)\*kWh\)\r\n')

		keys['+P'] = self.get_float(b'^1-0:1\.7\.0\(([0-9]+\.[0-9]+)\*kW\)\r\n')
		keys['-P'] = self.get_float(b'^1-0:2\.7\.0\(([0-9]+\.[0-9]+)\*kW\)\r\n')

		keys['V_1'] = self.get_float(b'^1-0:32\.7\.0\(([0-9]+\.[0-9]+)\*V\)\r\n')
		keys['V_2'] = self.get_float(b'^1-0:52\.7\.0\(([0-9]+\.[0-9]+)\*V\)\r\n')
		keys['V_3'] = self.get_float(b'^1-0:72\.7\.0\(([0-9]+\.[0-9]+)\*V\)\r\n')

		keys['+P_1'] = self.get_float(b'^1-0:21\.7\.0\(([0-9]+\.[0-9]+)\*kW\)\r\n')
		keys['+P_2'] = self.get_float(b'^1-0:41\.7\.0\(([0-9]+\.[0-9]+)\*kW\)\r\n')
		keys['+P_3'] = self.get_float(b'^1-0:61\.7\.0\(([0-9]+\.[0-9]+)\*kW\)\r\n')

		keys['-P_1'] = self.get_float(b'^1-0:22\.7\.0\(([0-9]+\.[0-9]+)\*kW\)\r\n')
		keys['-P_2'] = self.get_float(b'^1-0:42\.7\.0\(([0-9]+\.[0-9]+)\*kW\)\r\n')
		keys['-P_3'] = self.get_float(b'^1-0:62\.7\.0\(([0-9]+\.[0-9]+)\*kW\)\r\n')

		keys['+T'] = keys['+T1'] + keys['+T2']
		keys['-T'] = keys['-T1'] + keys['-T2']
		keys['P'] = keys['+P'] - keys['-P']

		keys['G'] = self.get_float(b'^(?:0-1:24\.2\.1(?:\(\d+[SW]\))?)?\(([0-9]{6}\.[0-9]{2})(?:\*m3)?\)\r\n', 0)

		self._keys = keys

	def __getitem__(self, key):
		return self._keys[key]

	def get_float(self, regex, default=None):
		result = self.get(regex, None)
		if not result:
			return default
		return float(self.get(regex, default))

	def get_int(self, regex, default=None):
		result = self.get(regex, None)
		if not result:
			return default
		return int(result)

	def get(self, regex, default=None):
		results = re.search(regex, self._datagram, re.MULTILINE)
		if not results:
			return default
		return results.group(1).decode('ascii')

	def validate(self):
		pattern = re.compile(b'\r\n(?=!)')
		for match in pattern.finditer(self._datagram):
			packet = self._datagram[:match.end() + 1]
			checksum = self._datagram[match.end() + 1:]

		if checksum.strip():
			given_checksum = int('0x' + checksum.decode('ascii').strip(), 16)
			calculated_checksum = crc16(packet)

			if given_checksum != calculated_checksum:
				raise P1PacketError('P1Packet with invalid checksum found')

	def __str__(self):
		return self._datagram.decode('ascii')


def send_to_influxdb(options, fields):
	req = {
		"measurement": options.influx_measurement,
		"tags"       : {},
		"fields"     : {}
	}

	if options.influx_tags is not None:
		for tag in options.influx_tags:
			tag_kv = tag.split('=')
			req['tags'][tag_kv[0]] = tag_kv[1]

	for field_k, field_v in fields.items():
		if field_v is not None:
			req['fields'][field_k] = field_v

	reqs = []
	reqs.append(req)

	client = InfluxDBClient(options.influx_hostname, options.influx_port, options.influx_username,
	                        options.influx_password, options.influx_database)
	client.write_points(reqs, retention_policy=options.influx_retention_policy, database=options.influx_database)


def start_monitor(options):
	meter = SmartMeter(options.device, options.baudrate)

	try:

		while True:
			datagram = meter.read_one_packet()
			send_to_influxdb(options, datagram._keys)

	finally:
		meter.disconnect()


if __name__ == "__main__":
	parser = ArgumentParser(description="Send P1 telegrams to an InfluxDB API")

	parser.add_argument("-d", "--device", dest="device", help="serial port to read datagrams from",
	                    default='/dev/ttyUSB0')
	parser.add_argument("-b", "--baudrate", dest="baudrate", help="baudrate for the serial connection",
	                    default='115200')

	influx_group = parser.add_argument_group()
	influx_group.add_argument("--influx-hostname", metavar='hostname', dest="influx_hostname",
	                          help="hostname to connect to InfluxDB, defaults to 'localhost'", default="localhost")
	influx_group.add_argument("--influx-port", metavar='port', dest="influx_port",
	                          help="port to connect to InfluxDB, defaults to 8086", type=int, default=8086)
	influx_group.add_argument("--influx-username", metavar='username', dest="influx_username",
	                          help="user to connect, defaults to 'root'", default="root")
	influx_group.add_argument("--influx-password", metavar='password', dest="influx_password",
	                          help="password of the user, defaults to 'root'", default="root")
	influx_group.add_argument("--influx-database", metavar='dbname', dest="influx_database",
	                          help="database name to connect to, defaults to 'p1smartmeter'", default="p1smartmeter")
	influx_group.add_argument("--influx-retention-policy", metavar='policy', dest="influx_retention_policy",
	                          help="retention policy to use")

	influx_group.add_argument("--influx-measurement", metavar='measurement', dest="influx_measurement",
	                          help="measurement name to store points, defaults to smartmeter", default="smartmeter")
	influx_group.add_argument('influx_tags', metavar='tag ...', type=str, nargs='?', help='any tag to the measurement')

	verbose_group = parser.add_mutually_exclusive_group()
	verbose_group.add_argument("-v", "--verbose", action="store_true", dest="verbose", help="Be verbose")
	verbose_group.add_argument("-q", "--quiet", action="store_true", dest="quiet", help="Be very quiet")

	args = parser.parse_args()

	start_monitor(args)
