import time
import serial
import psutil
import platform
import serial.tools.list_ports
from datetime import datetime
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
import matplotlib.dates as mdates
import matplotlib.gridspec as gridspec
import logging
import os

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Detect operating system
IS_WINDOWS = platform.system() == 'Windows'
IS_LINUX = platform.system() == 'Linux'

# Settings for the Elmor Labs PMD sensor connection
PMD_SETTINGS = {
    'port': None,  # Dynamically assigned
    'baudrate': 115200,
    'bytesize': 8,
    'stopbits': 1,
    'timeout': 1,
}

# Configuration flags
LIST_ALL_WINDOWS_PORTS = True  # Set to True to list all available COM ports
SAVE_TO_CSV = True  # Set to True to save the power data to a CSV file
MAX_LENGTH = 1000  # Maximum number of data points to retain in memory
PROCESS_NAMES = ['rstudio.exe', 'rsession-utf8.exe']
NUM_CORES = psutil.cpu_count()  # Get the number of CPU cores

# Initialize a global DataFrame for storing sensor data
df = pd.DataFrame(columns=['timestamp', 'id', 'unit', 'Power', 'Voltage', 'Current'])
date_name = datetime.now().strftime('%y%m%d-%H%M')

# Define global variables for plot axes
voltage_ax = None
current_ax = None
power_ax = None


def list_ports():
    """Lists all available COM or ttyUSB ports."""
    ports = list(serial.tools.list_ports.comports())
    print('Available ports:')
    for p in ports:
        print(f'{p.device} - {p.description}')
    print()


def detect_serial_port():
    """Detects the serial port based on the operating system and device description."""
    ports = list(serial.tools.list_ports.comports())

    # Define the target device description (e.g., USB-SERIAL CH340)
    target_device_description = ['USB-SERIAL', 'USB Serial']

    if IS_WINDOWS:
        # For Windows, look for a device matching the target descriptions
        for port in ports:
            if any(desc in port.description for desc in target_device_description):
                return port.device  # Return COMx port for Windows

    elif IS_LINUX:
        # For Linux, look for devices like /dev/ttyUSBx or /dev/ttySx
        for port in ports:
            # Check description and device name (typically /dev/ttyUSBx or /dev/ttySx)
            if any(desc in port.description for desc in target_device_description):
                return port.device  # Return /dev/ttyUSBx or /dev/ttySx for Linux

    print("No appropriate port found. Listing available ports:")
    list_ports()  # List all ports for debugging
    return None  # If no suitable port is found


def get_cpu_usage(process_names: list) -> float:
    """Gets the combined CPU usage of a list of processes."""
    total_cpu_usage = 0.0

    for process_name in process_names:
        for process in psutil.process_iter(['pid', 'name']):
            if process.info['name'] == process_name:
                try:
                    cpu_usage = psutil.Process(process.info['pid']).cpu_percent(interval=1)
                    total_cpu_usage += cpu_usage
                    logging.debug(f'CPU usage for {process_name}: {cpu_usage}%')
                except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                    logging.error(f'Error accessing process {process_name}: {e}')
    
    return total_cpu_usage



def normalize_cpu_usage(cpu_usage: float, num_cores: int) -> float:
    """Normalizes CPU usage to a range of 0-100% considering the number of cores."""
    normalized = min(max(cpu_usage / num_cores, 0.0), 100.0)
    logging.debug(f'Normalized CPU usage: {normalized}%')
    return normalized


def check_connection() -> None:
    """Checks the connection with the Elmor Labs PMD sensor."""
    PMD_SETTINGS['port'] = detect_serial_port()

    if PMD_SETTINGS['port'] is None:
        logging.error("No serial port detected.")
        return

    try:
        with serial.Serial(**PMD_SETTINGS) as ser:
            ser.write(b'\x00')  # Send a command to the sensor
            ser.flush()  # Ensure all data is sent
            read_bytes = ser.read(18)  # Read the welcome message
            assert read_bytes == b'ElmorLabs PMD-USB', "Incorrect welcome message received"

            ser.write(b'\x02')  # Send another command to the sensor
            ser.flush()
            ser.read(100)  # Read additional data
        logging.info("Connection with PMD sensor established successfully.")
    except (serial.SerialException, AssertionError) as e:
        logging.error(f"Failed to establish connection with PMD sensor: {e}")


def get_new_sensor_values() -> pd.DataFrame:
    """Gets new sensor values from the Elmor Labs PMD and stores them in a DataFrame."""
    if PMD_SETTINGS['port'] is None:
        logging.error("Serial port not set. Cannot get new sensor values.")
        return pd.DataFrame()

    try:
        with serial.Serial(**PMD_SETTINGS) as ser:
            command = b'\x03'  # Command to request sensor data
            ser.write(command)  # Send the command
            ser.flush()  # Ensure all data is sent
            read_bytes = ser.read(16)  # Read sensor data

        # Capture the current timestamp
        timestamp = pd.Timestamp(datetime.now())

        # Process the received sensor data
        i = 2  # Index for reading values
        name = 'EPS1'  # Sensor name
        voltage_value = int.from_bytes(read_bytes[i * 4:i * 4 + 2], byteorder='little') * 0.01
        current_value = int.from_bytes(read_bytes[i * 4 + 2:i * 4 + 4], byteorder='little') * 0.1

        # Get CPU usage for the list of processes
        cpu_usage = get_cpu_usage(PROCESS_NAMES)
        cpu_usage_normalized = normalize_cpu_usage(cpu_usage, NUM_CORES)
        power_value = max((voltage_value * current_value * cpu_usage_normalized) / 100, 0.0)
        power_value = round(power_value, 4)

        logging.debug(f"Collected data - Power: {power_value} W, Voltage: {voltage_value} V, Current: {current_value} A")

        # Create DataFrame with new sensor data
        data = {
            'timestamp': [timestamp, timestamp, timestamp],
            'id': [name, name, name],
            'unit': ['P', 'U', 'I'],
            'Power': [power_value, None, None],
            'Voltage': [None, voltage_value, None],
            'Current': [None, None, current_value],
        }

        df_new = pd.DataFrame(data)
        return df_new

    except serial.SerialException as e:
        logging.error(f"Serial communication error: {e}")
        return pd.DataFrame()

def animation_update(frame):
    """Updates the plot with new sensor data."""
    global df

    # Get new sensor data
    df_new_data = get_new_sensor_values()

    # Clean the DataFrame by removing rows and columns that are completely NA
    df_new_data_clean = df_new_data.dropna(how='all').dropna(axis=1, how='all')

    if not df_new_data_clean.empty and df_new_data_clean.shape[1] > 0:
        # Concatenate only if the DataFrame is not empty after cleaning
        df = pd.concat([df, df_new_data_clean], ignore_index=True)
        logging.debug("New valid data appended to DataFrame.")
    else:
        logging.debug("No valid new data to append.")

    # Trim DataFrame to the last MAX_LENGTH rows if necessary
    if df.shape[0] > MAX_LENGTH:
        df = df.iloc[-MAX_LENGTH:]
        logging.debug("DataFrame trimmed to maintain maximum length.")

    # Prepare data for plotting
    df_power_plot = df[df.unit == 'P'].pivot(columns=['id'], index='timestamp', values='Power')
    df_voltage_plot = df[df.unit == 'U'].pivot(columns=['id'], index='timestamp', values='Voltage')
    df_current_plot = df[df.unit == 'I'].pivot(columns=['id'], index='timestamp', values='Current')

    # Clear the axes for redrawing
    power_ax.cla()
    voltage_ax.cla()
    current_ax.cla()

    # Plot the data
    df_power_plot.plot(ax=power_ax, legend=False, color='red', linewidth=2)
    df_voltage_plot.plot(ax=voltage_ax, legend=False, color='blue', linewidth=2)
    df_current_plot.plot(ax=current_ax, legend=False, color='green', linewidth=2)

    # Set axis labels and titles
    power_ax.set_ylabel('Power Consumption [W]', fontsize=12, color='red')
    voltage_ax.set_ylabel('Voltage [V]', fontsize=12, color='blue')
    current_ax.set_ylabel('Current [A]', fontsize=12, color='green')

    # Set titles for each plot
    power_ax.set_title('Real-Time MATLAB Power Consumption', fontsize=14, color='red')
    voltage_ax.set_title('Real-Time CPU Voltage Consumption', fontsize=14, color='blue')
    current_ax.set_title('Real-Time CPU Current Consumption', fontsize=14, color='green')

    # Display gridlines
    power_ax.grid(True, which='both', linestyle='--', linewidth=0.5)
    voltage_ax.grid(True, which='both', linestyle='--', linewidth=0.5)
    current_ax.grid(True, which='both', linestyle='--', linewidth=0.5)

    # Format x-axis to display time correctly
    power_ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    voltage_ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    current_ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))

    # Rotate date labels for better readability
    plt.setp(power_ax.xaxis.get_majorticklabels(), rotation=45, ha='right')
    plt.setp(voltage_ax.xaxis.get_majorticklabels(), rotation=45, ha='right')
    plt.setp(current_ax.xaxis.get_majorticklabels(), rotation=45, ha='right')

    # Auto adjust plot limits with buffer
    buffer = 1e-6  # Small buffer to avoid singular transformations

    # For power axis
    if not df_power_plot.empty:
        power_ymin = df_power_plot.min().min() * 0.9 - buffer
        power_ymax = df_power_plot.max().max() * 1.1 + buffer
        power_ax.set_ylim(power_ymin, power_ymax)

    # For voltage axis
    if not df_voltage_plot.empty:
        voltage_ymin = df_voltage_plot.min().min() * 0.9 - buffer
        voltage_ymax = df_voltage_plot.max().max() * 1.1 + buffer
        voltage_ax.set_ylim(voltage_ymin, voltage_ymax)

    # For current axis
    if not df_current_plot.empty:
        current_ymin = df_current_plot.min().min() * 0.9 - buffer
        current_ymax = df_current_plot.max().max() * 1.1 + buffer
        current_ax.set_ylim(current_ymin, current_ymax)

    # Tighten layout to avoid overlapping
    fig.tight_layout()

    # Save data to CSV if enabled
    if SAVE_TO_CSV:
        save_data_to_csv(df)


def save_data_to_csv(df: pd.DataFrame) -> None:
    """Saves the power data to a CSV file."""
    global date_name
    try:
        # Ensure the 'data' directory exists
        os.makedirs('./data', exist_ok=True)

        # Filter only for valid Power entries
        df_power = df[['timestamp', 'id', 'unit', 'Power']].dropna(subset=['Power'])

        # Save to CSV
        file_path = f'./data/{date_name}_measurements.csv'  # Use a generic path
        df_power.to_csv(file_path, mode='w', header=True, index=False)
        logging.info(f"Data saved to {file_path}")
    except Exception as e:
        logging.error(f"Error saving power data to CSV: {e}")


if __name__ == "__main__":
    if LIST_ALL_WINDOWS_PORTS:
        list_ports()

    check_connection()

    plt.style.use('ggplot')

    # Define and adjust figure with gridspec for different subplot sizes
    fig = plt.figure(figsize=(10, 16), facecolor='#707576')
    gs = gridspec.GridSpec(3, 1, height_ratios=[1, 1, 1])
    voltage_ax = plt.subplot(gs[0])
    current_ax = plt.subplot(gs[1])
    power_ax = plt.subplot(gs[2])

    fig.suptitle('Measurement CPU and MATLAB', fontsize=14)
    anim = FuncAnimation(fig, animation_update, interval=1000, cache_frame_data=False)
    fig.tight_layout()
    fig.subplots_adjust(left=0.09)
    plt.show()
