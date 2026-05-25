import os
import subprocess
import sys
import re
import struct
from colorama import init, Fore, Style

init(autoreset=True)

def check_ffmpeg_dependencies():
    for binary in ["ffmpeg", "ffprobe"]:
        try:
            subprocess.run([binary, "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            print(f"\n{Fore.RED}{Style.BRIGHT} CRITICAL ERROR: '{binary}' was not detected on your PC!")
            print(f"{Fore.YELLOW}Ensure FFmpeg is installed and added to your system's Environment Variables (PATH).\n")
            sys.exit(1)

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def get_video_duration(video_path):
    cmd = [
        "ffprobe", "-v", "error", 
        "-show_entries", "format=duration", 
        "-of", "default=noprint_wrappers=1:nokey=1", 
        video_path
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        total_seconds = float(result.stdout.strip())
        return total_seconds, int(total_seconds // 60), int(total_seconds % 60)
    except Exception:
        return 0.0, 0, 0

def convert_time_to_seconds(time_str):
    time_str = time_str.strip()
    match = re.match(r"^(\d+):([0-5]?\d)$", time_str)
    if match:
        mins, secs = map(int, match.groups())
        return (mins * 60) + secs
    return None

def convert_mp4_to_avi(input_path, output_avi, gop):
    print(f"\n{Fore.CYAN}[+] Converting MP4 to glitch-ready AVI (GOP Gap: {gop})...")
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vcodec", "mpeg4", 
        "-g", str(gop), 
        "-qscale:v", "2", 
        output_avi
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return True
    except subprocess.CalledProcessError:
        return False

def parse_avi_index(data):
    keyframes = set()
    idx1_pos = data.rfind(b'idx1')
    if idx1_pos == -1:
        return keyframes
    
    idx_chunk_size = struct.unpack('<I', data[idx1_pos+4:idx1_pos+8])[0]
    entry_start = idx1_pos + 8
    entry_end = entry_start + idx_chunk_size
    
    current = entry_start
    movi_pos = data.find(b'movi')
    if movi_pos == -1:
        return keyframes
    movi_data_start = movi_pos + 4

    while current + 16 <= entry_end:
        chunk_id = data[current:current+4]
        flags = struct.unpack('<I', data[current+4:current+8])[0]
        offset = struct.unpack('<I', data[current+8:current+12])[0]
        
        if chunk_id == b'00dc':
            absolute_offset = movi_data_start + offset
            if flags & 0x10:
                keyframes.add(absolute_offset)
        current += 16
        
    return keyframes

def execute_native_mosh(avi_path, options):
    print(f"\n{Fore.CYAN}[+] Opening AVI byte stream natively...")
    
    with open(avi_path, 'rb') as f:
        data = f.read()

    frame_marker = b'\x30\x30\x64\x63' 
    frame_indices = [m.start() for m in re.finditer(frame_marker, data)]
    total_frames = len(frame_indices)
    
    if total_frames == 0:
        print(f"{Fore.YELLOW} Warning: Could not map raw video frame markers. Glitch reduced.")
        return False

    print(f"{Fore.CYAN}[+] Analyzing AVI structural index table...")
    keyframe_offsets = parse_avi_index(data)

    raw_start = int(options["start_sec"] * 30)
    start_frame = min(raw_start, total_frames - 1)
    if start_frame == 0:
        start_frame = 1

    end_frame = min(int(options["end_sec"] * 30), total_frames - 1)
    if start_frame >= end_frame:
        end_frame = total_frames - 1

    print(f"{Fore.MAGENTA}[+] Corrupting frames between target range: #{start_frame} to #{end_frame}")

    mode = options["mode"]
    output_bytes = bytearray()
    output_bytes.extend(data[:frame_indices[start_frame]])
    
    glide_saved_chunk = None
    glide_counter = 0
    stutter_buffer = []

    for i in range(start_frame, end_frame):
        start_pos = frame_indices[i]
        end_pos = frame_indices[i+1] if i+1 < total_frames else len(data)
        frame_chunk = data[start_pos:end_pos]
        frame_size = len(frame_chunk)

        if start_pos in keyframe_offsets:
            output_bytes.extend(frame_chunk)
            continue

        if mode == "AutoMosh":
            threshold = options["kill_frame_size"]
            if threshold == 0 or frame_size <= threshold:
                if frame_size > 8:
                    output_bytes.extend(frame_chunk[:8] + b'\x00' * (frame_size - 8))
                else:
                    output_bytes.extend(b'\x00' * frame_size)
            else:
                output_bytes.extend(frame_chunk)

        elif mode == "Classic":
            if i % (options["delta"] + 1) == 0:
                output_bytes.extend(frame_chunk)
            else:
                pass

        elif mode == "Glide":
            if glide_saved_chunk is None:
                glide_saved_chunk = frame_chunk
                
            if glide_counter < options["glide_intensity"]:
                output_bytes.extend(glide_saved_chunk)
                glide_counter += 1
            else:
                glide_saved_chunk = frame_chunk
                output_bytes.extend(frame_chunk)
                glide_counter = 0

        elif mode == "Repetition":
            stutter_buffer.append(frame_chunk)
            if len(stutter_buffer) > 12:
                stutter_buffer.pop(0)
            
            if i % 12 == 0 and len(stutter_buffer) == 12:
                for repeating_chunk in stutter_buffer:
                    output_bytes.extend(repeating_chunk)
            else:
                output_bytes.extend(frame_chunk)

    if end_frame < total_frames:
        output_bytes.extend(data[frame_indices[end_frame]:])

    print(f"{Fore.CYAN}[+] Saving modified bytes back to file...")
    with open(avi_path, 'wb') as f:
        f.write(output_bytes)
    return True

def fix_and_convert_to_mp4(input_avi, final_mp4, quality, speed_factor):
    print(f"\n{Fore.CYAN}[+] Re-compiling, scaling speed, and repairing frame indexes into safe MP4...")
    
    setpts_val = 1.0 / speed_factor
    filter_str = f"setpts={setpts_val}*PTS"
    
    cmd = [
        "ffmpeg", "-y", "-i", input_avi,
        "-vf", filter_str,
        "-vcodec", "libx264", 
        "-crf", str(quality), 
        "-pix_fmt", "yuv420p", 
        final_mp4
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return True
    except subprocess.CalledProcessError:
        return False

def main():
    check_ffmpeg_dependencies()
    clear_screen()
    print(f"{Fore.GREEN}{Style.BRIGHT}Datamosher Free Early Access (Every effect is free)")
    print(f"{Fore.RED}Due to early access this isnt in alpha neither on beta this tool is mid development      ")
    print(f"{Fore.RED}you have early access to it and it contains alot of bugs                                 \n")
    input_video = input(f"{Fore.BLUE}Enter path to input MP4 video: ").strip('"').strip("'")
    if not os.path.exists(input_video):
        print(f"{Fore.RED} File not found!")
        sys.exit(1)
        
    total_seconds, max_min, max_sec = get_video_duration(input_video)
    scanner_failed = (total_seconds == 0.0)
    max_time_str = "Unknown" if scanner_failed else f"{max_min:02d}:{max_sec:02d}"
    
    if not scanner_failed:
        print(f"{Fore.GREEN} Video detected! Total Length: {max_time_str}")

    output_dir = os.path.dirname(os.path.abspath(input_video))
    temp_avi = os.path.join(output_dir, "temp_datamosh_holding.avi")
    final_mp4 = os.path.join(output_dir, "moshed_output.mp4")

    print(f"\n{Fore.YELLOW}[1] Select Datamosh Method:")
    print(f"{Fore.WHITE}1. AutoMosh (Trendy Datamosh)")
    print(f"{Fore.WHITE}2. Classic (Traditional Datamosh)")
    print(f"{Fore.WHITE}3. Glide (Gliding Pixels)")
    print(f"{Fore.WHITE}4. Repetition (Range Repetition)")
    mode_choice = input(f"{Fore.BLUE}Choice (1-4): ").strip()
    modes = {"1": "AutoMosh", "2": "Classic", "3": "Glide", "4": "Repetition"}
    selected_mode = modes.get(mode_choice, "AutoMosh")

    print(f"\n{Fore.YELLOW}[2] Configure Parameters for {selected_mode}:")
    
    kill_frame_size = 0
    if selected_mode == "AutoMosh":
        kill_frame_size = int(input(f"{Fore.BLUE} -> Enter Kill Frame Size Limit (Bytes, 0 for all, Default 0): ") or 0)
        
    glide_intensity = 0
    if selected_mode == "Glide":
        glide_intensity = int(input(f"{Fore.BLUE} -> Enter Glide Pixels Intensity (Default 10): ") or 10)

    classic_speed = 1.0
    if selected_mode == "Classic":
        while True:
            try:
                classic_speed = float(input(f"{Fore.BLUE} -> Enter Playback Speed (0.5 to 2.0, Default 1.0): ") or 1.0)
                if 0.5 <= classic_speed <= 2.0:
                    break
                print(f"{Fore.RED} Please stick strictly within the 0.5x to 2.0x limit range.")
            except ValueError:
                print(f"{Fore.RED} Invalid number format.")

    start_sec, end_sec = 0.0, total_seconds
    print(f"\n{Fore.YELLOW} Mosh Window Time Boundaries")
    while True:
        start_input = input(f"{Fore.BLUE} -> Start Effect Time (MM:SS, Default 00:00): ") or "00:00"
        parsed_start = convert_time_to_seconds(start_input)
        if parsed_start is not None:
            start_sec = parsed_start
            break
        print(f"{Fore.RED} Invalid format.")

    while True:
        default_end = "00:10" if scanner_failed else max_time_str
        end_input = input(f"{Fore.BLUE} -> End Effect Time (MM:SS, Default {default_end}): ") or default_end
        parsed_end = convert_time_to_seconds(end_input)
        if parsed_end is not None and parsed_end >= start_sec:
            end_sec = parsed_end
            break
        print(f"{Fore.RED} Invalid input.")

    quality = int(input(f"\n{Fore.BLUE} -> Enter Fixed Quality / CRF (1-51, Default 23): ") or 23)
    delta_value = int(input(f"{Fore.BLUE} -> Enter Delta Value / Frame Skip (Default 3): ") or 3)
    gop_value = int(input(f"{Fore.BLUE} -> Enter GOP Value / Keyframe Gap (Lower = More Aggressive, Default 45): ") or 45)

    mosh_options = {
        "mode": selected_mode,
        "kill_frame_size": kill_frame_size,
        "glide_intensity": glide_intensity,
        "start_sec": start_sec,
        "end_sec": end_sec,
        "delta": delta_value
    }

    if convert_mp4_to_avi(input_video, temp_avi, gop_value):
        execute_native_mosh(temp_avi, mosh_options)
        if fix_and_convert_to_mp4(temp_avi, final_mp4, quality, classic_speed):
            print(f"\n{Fore.GREEN}{Style.BRIGHT} SUCCESS! Moshed file exported to: {final_mp4}")
        else:
            print(f"{Fore.RED} Error repairing files back to MP4 container structure.")
    else:
        print(f"{Fore.RED} Error converting target format container file.")

    if os.path.exists(temp_avi):
        os.remove(temp_avi)

if __name__ == "__main__":
    main()