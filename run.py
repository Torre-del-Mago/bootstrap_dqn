import subprocess
import sys

def run_script_instances(script_path, num_instances, args_list):
    processes = []

    for i in range(num_instances):
        cmd = [sys.executable, script_path] + args_list[i]
        process = subprocess.Popen(cmd)
        processes.append(process)
        print(f"Started instance {i + 1} with arguments: {args_list[i]}")

if __name__ == "__main__":
    script_path = 'run_bootstrap.py'
    num_instances = 4

    # List of arguments for each instance
    args_list = [
        ['-c', '0', '-h', '10', '-v', '1'],
        ['-c', '1', '-h', '10', '-v', '3'],
        ['-c', '2', '-h', '10', '-v', '5'],
        ['-c', '3', '-h', '10', '-v', '10']
    ]

    run_script_instances(script_path, num_instances, args_list)
    print("Parent process finished. Child processes are running in the background.")