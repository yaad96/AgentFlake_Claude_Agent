#!/usr/bin/env python3

import os
import sys


EVENTS_MAP = {}
accept_short_id = set()

def read_events_map():
    global EVENTS_MAP
    with open('events_encoding_id.txt') as f:
        for line in f.readlines():
            line = line.strip().split(',')
            if len(line) == 3:
                EVENTS_MAP['e{}'.format(line[2])] = line[1]


def read_locations(locations_txt):
    locations = {}
    header = False
    # Create blank file if it doesn't exist
    if not os.path.exists(locations_txt):
        with open(locations_txt, 'w') as f:
            f.write('id location\n')  # Write a header for the blank file
        return locations
    with open(locations_txt) as f:
        for line in f.readlines():
            if not header:
                header = True
                continue
            line = line.strip()
            if line:
                id, _, code = line.partition(' ')
                if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    if code and ')' in code and id.isdigit():
                    locations[id] = code[:code.index(')') + 1]
    return locations


def is_event_id(name):
    return name[0] == 'e' and name[1] in [str(i) for i in range(0, 10)]


def read_unique_traces(traces_txt, convert=None):
    has_id = False
    traces = {}  # Map trace to frequency
    header = False
    convert_event = 1 # 1 is unsure, 2 is sure, 0 is no
    
    # Create blank file if it doesn't exist
    if not os.path.exists(traces_txt):
        with open(traces_txt, 'w') as f:
            f.write('trace frequency trace\n')  # Write a header for the blank file
        return traces
    
    with open(traces_txt) as f:
        for line in f.readlines():
            line = line.strip()
            if line:
                if not header:
                    header = True
                    if 'WITH ID' in line:
                        has_id = True
                    continue

                trace_id = -1
                if has_id:
                    # line is trace-id trace-frequency trace
                    trace_id, _, line = line.partition(' ')
                freq, _, trace = line.partition(' ')
                if convert is not None:
                    t = trace[1:-1] # Turn "[a~1, b~2]" to "a~1, b~2"
                    new_trace = []
                    skip = True
                    for e in t.split(', '):
                        e_name, _, e_loc_freq = e.partition('~')

                        if has_id and convert_event > 0:
                            # need to convert ID (e.g., e1, e2, etc.) to spec event name (e.g., has_next, next)
                            if convert_event == 1:
                                convert_event = 2 if is_event_id(e_name) else 0
                                if convert_event == 2:
                                    e_name = EVENTS_MAP[e_name]
                            else:
                                e_name = EVENTS_MAP[e_name]

                        e_loc, _, e_freq = e_loc_freq.partition('x')
                        if not e_freq:
                            e_freq = 1
                        for actual_short, global_short in convert.items():
                            if actual_short == e_loc:
                                if e_freq == 1:
                                    new_trace.append('{}~{}'.format(e_name, global_short))
                                else:
                                    e_name = '{}~{}'.format(e_name, global_short)
                                    for i in range(int(e_freq)):
                                        new_trace.append(e_name)
                                skip = False
                                break
                    if skip:
                        print('will skip ' + t + ' because event location mismatch (actual)')
                    if '[' + ', '.join(new_trace) + ']' not in traces:
                        traces['[' + ', '.join(new_trace) + ']'] = [0, trace_id]
                    traces['[' + ', '.join(new_trace) + ']'][0] = traces.get('[' + ', '.join(new_trace) + ']', 0)[0] + int(freq)
                    continue
                else:
                    t = trace[1:-1] # Turn "[a~1, b~2]" to "a~1, b~2"
                    new_trace = []
                    skip = False
                    for e in t.split(', '):
                        e_name, _, e_loc_freq = e.partition('~')

                        if has_id and convert_event > 0:
                            # need to convert ID (e.g., e1, e2, etc.) to spec event name (e.g., has_next, next)
                            if convert_event == 1:
                                convert_event = 2 if is_event_id(e_name) else 0
                                if convert_event == 2:
                                    e_name = EVENTS_MAP[e_name]
                            else:
                                e_name = EVENTS_MAP[e_name]

                        e_loc, _, e_freq = e_loc_freq.partition('x')
                        if not e_freq:
                            new_trace.append('{}~{}'.format(e_name, e_loc))
                        else:
                            e_name = '{}~{}'.format(e_name, e_loc)
                            for i in range(int(e_freq)):
                                new_trace.append(e_name)
                        if e_loc not in accept_short_id:
                            skip = True
                    if skip:
                        print('will skip ' + t + ' because event location mismatch (expected)')
                    if '[' + ', '.join(new_trace) + ']' not in traces:
                        traces['[' + ', '.join(new_trace) + ']'] = [0, trace_id]
                    traces['[' + ', '.join(new_trace) + ']'][0] = traces.get('[' + ', '.join(new_trace) + ']', 0)[0] + int(freq)
                    continue
    return traces


def read_test(specs_test_csv):
    trace_id = 0
    trace_id_to_test = {}
    with open(specs_test_csv) as f:
        for line in f.readlines():
            line = line.strip()
            if line and line != 'OK':
                t_id, _, tests = line.partition(' ')
                trace_id_to_test[str(trace_id)] = tests
                trace_id += 1
    return trace_id_to_test
        

def compact_trace(trace):
    trace = trace[1:-1] # remove []
    events = trace.split(', ')
    final = [] # store [event name, freq] pair
    last_event = 'N/A'
    for event in events:
        if event == last_event: # if this event is the same as last event
            final[-1][1] = final[-1][1] + 1 # then, find last event, add 1 to freq
        else:
            final.append([event, 1]) # otherwise, insert new event to final list and set freq to 1
            last_event = event # and update last_event
    final_str = []
    for i in final:
        if i[1] > 1:
            final_str.append('{}x{}'.format(i[0], i[1]))
        else:
            final_str.append('{}'.format(i[0]))
    return '[' + ', '.join(final_str) + ']'
        

def compare(actual, expected, list_test=False):
    actual_locations = read_locations(os.path.join(actual, 'locations.txt'))
    expected_locations = read_locations(os.path.join(expected, 'locations.txt'))
    ignore = set()
    if list(sorted(actual_locations.values())) != list(sorted(expected_locations.values())):
        print('ERROR:\t\tLocations don\'t match')
        ignore = set(sorted(actual_locations.values())).symmetric_difference(set(sorted(expected_locations.values())))
        if ignore:
            print(ignore)

    # Create a single location map
    global_location = {} # Long location to global short location
    actual_short_to_global = {} # Actual locations' short location to expected's short location
    global accept_short_id
    for short, long in expected_locations.items():
        if long in ignore:
            continue
        global_location[long] = short
        accept_short_id.add(short)
    for short, long in actual_locations.items():
        # If long is xxx, short is y, and global_location[xxx] is z
        # It means in expected_locations, z points to xxx
        # So we want to convert actual_locations such that z points to xxx a well
        if long in ignore:
            continue
        actual_short_to_global[short] = global_location[long]  # This will map y to z


    actual_traces = read_unique_traces(os.path.join(actual, 'unique-traces.txt'), actual_short_to_global)
    expected_traces = read_unique_traces(os.path.join(expected, 'unique-traces.txt'))
    
    
    if list_test:
        actual_tests = read_test(os.path.join(actual, 'specs-test.csv'))
        expected_tests = read_test(os.path.join(expected, 'specs-test.csv'))

    for expected_trace, expected_frequency in expected_traces.items():
        if expected_trace not in actual_traces:
            print('ERROR:\t\t{} (ID: {}) is in expected ({} times) but not actual'.format(compact_trace(expected_trace), expected_frequency[1], expected_frequency[0]))
            if list_test:
                print('\t\tTest in expected that has this trace: ' + expected_tests.get(expected_frequency[1]))
        elif expected_frequency[0] != actual_traces[expected_trace][0]:
            print('WARNING:\t\t{}\'s (ID: {}) frequency is {} in expected, but is {} (ID: {}) in actual'.format(compact_trace(expected_trace), expected_frequency[1], expected_frequency[0], actual_traces[expected_trace][0], actual_traces[expected_trace][1]))
            if list_test:
                print('\t\tTest in expected that has this trace: ' + expected_tests.get(expected_frequency[1]))
                print('\t\tTest in actual that has this trace: ' + actual_tests.get(actual_traces[expected_trace][1]))
    for actual_trace, actual_frequency in actual_traces.items():
        if actual_trace not in expected_traces:
            print('ERROR:\t\t{} (ID: {}) is in actual ({} times) but not expected'.format(compact_trace(actual_trace), actual_frequency[1], actual_frequency[0]))
            if list_test:
                print('\t\tTest in actual that has this trace: ' + actual_tests.get(actual_frequency[1]))



def main(argv=None):
    argv = argv or sys.argv

    if len(argv) < 3:
        print('Usage: python3 compare-traces.py <actual-traces-dir> <expected-traces-dir> [list-test: true/false]')
        exit(1)
    actual = argv[1]
    expected = argv[2]
    
    list_test = False
    if len(argv) == 4:
        list_test = argv[3] == 'true'

    # Create blank files if they don't exist (for handling missing traces directories)
    for traces_dir in [actual, expected]:
        for fname in ['locations.txt', 'unique-traces.txt']:
            fpath = os.path.join(traces_dir, fname)
            if not os.path.exists(fpath):
                os.makedirs(traces_dir, exist_ok=True)
                with open(fpath, 'w') as f:
                    if fname == 'locations.txt':
                        f.write('id location\n')
                    elif fname == 'unique-traces.txt':
                        f.write('trace frequency trace\n')

    read_events_map()
    compare(actual, expected, list_test)


if __name__ == '__main__':
    main()
