import csv
import json
import uuid
import subprocess
import argparse
import re
from datetime import datetime
from operator import itemgetter
import gitlab
from openai import OpenAI
import config
from tenacity import retry, stop_after_attempt, wait_random_exponential  # for exponential backoff


def convert_time_format(at):
    return datetime.strptime(at, '%Y-%m-%dT%H:%M:%S.%fZ').strftime('%d-%m-%Y %H:%M:%S.%f')


def save_dict_to_json(data, file_path):
    with open(file_path, 'w') as f:
        json.dump(data, f, indent=4)


def load_json_to_dict(file_path):
    """
    Load a JSON file into a dictionary.

    Parameters:
    file_path (str): The path to the JSON file.

    Returns:
    dict: The dictionary loaded from the JSON file.
    """
    try:
        with open(file_path, 'r') as file:
            return json.load(file)
    except Exception as e:
        print(f"Error loading JSON file: {e}")
        return {}


def extract_gitlab_conversations(args):
    personal_access_token = args.gitlab_access_token
    gl = gitlab.Gitlab(config.GITLAB_URL, private_token=personal_access_token)
    project = gl.projects.get(config.PROJECT_PATH)
    project_web_url = project.web_url
    page = 1
    per_page = 20
    merged_mr_count = 0
    reviewed_merged_mr_count = set()
    conversation_data = []
    while True:
        mr_params = {
            'state': 'merged',
            'page': page,
            'per_page': per_page
        }
        if args.reviewed_username:
            mr_params['author_username'] = args.reviewed_username

        merge_requests = project.mergerequests.list(**mr_params)
        if not merge_requests:
            break

        for merge_request in merge_requests:
            merged_mr_count += 1

            if not merge_request.user_notes_count:
                continue

            discussions = merge_request.discussions.list(get_all=True)

            for discussion in discussions:
                conversation = filter_and_sort_notes(args.reviewer_username, args.reviewed_username,
                                                     discussion.attributes['notes'])
                if conversation:
                    reviewed_merged_mr_count.add(merge_request.iid)
                    data = collect_conversation_data(conversation)
                    data[
                        'conversation_link'] = f"{project_web_url}/-/merge_requests/{merge_request.iid}/{data['conversation_link']}"
                    data['notes'] = get_notes(conversation)
                    data['diff_text'] = None
                    data['discussion_range'] = {'start': {'line': None, 'type': None},
                                                'end': {'line': None, 'type': None}}
                    first_note_position = conversation[0].get('position')
                    if first_note_position:
                        git_diff = get_git_diff(first_note_position['base_sha'], first_note_position['head_sha'],
                                                first_note_position['new_path'])
                        if git_diff:
                            data['diff_text'] = git_diff
                        start_line, start_line_type = get_line_and_type_from_position(first_note_position, 'start')
                        end_line, end_line_type = get_line_and_type_from_position(first_note_position, 'end')
                        data['discussion_range'] = {'start': {'line': start_line, 'type': start_line_type},
                                                    'end': {'line': end_line, 'type': end_line_type}}
                    # start_position = first_note_position['line_range']['start']
                    # data['reference_code'] = extract_line_from_diff(diff_text, start_position['new_line'], start_position['type'])

                    conversation_data.append(data)

        page += 1
    print(f'----------------\n\n')
    print(
        f'Total merged merge requests: {merged_mr_count}. Total reviewed merge requests: {len(reviewed_merged_mr_count)}')
    print(f'----------------\n\n')
    return conversation_data


def parse_gitlab_previous_output(file_path):
    conversation_data = []

    with open(file_path, 'r') as file:
        lines = file.readlines()

    current_conversation = {}
    reviewers = set()
    first_note_date = None

    for line in lines:
        line = line.strip()
        if line.startswith('Conversation Link:'):
            if current_conversation:
                current_conversation['note_count'] = len(current_conversation.get('notes', []))
                current_conversation['reviewers'] = ', '.join(reviewers)
                conversation_data.append(current_conversation)
            current_conversation = {'notes': []}
            reviewers = set()
            first_note_date = None
            current_conversation['conversation_link'] = line.split(' ')[-1]
        elif not re.match(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z - ', line):
            current_conversation['notes'][-1]['message'] += f'\n{line}'
        elif line:
            timestamp, author_message = line.split(' - ', 1)
            author = author_message.split(': ')[0]
            reviewers.add(author)

            if not first_note_date:
                first_note_date = datetime.strptime(timestamp, '%Y-%m-%dT%H:%M:%S.%fZ')
                current_conversation['first_note_date'] = first_note_date.strftime('%d-%m-%Y %H:%M:%S.%f')

            current_conversation['notes'].append({'timestamp': timestamp, 'message': author_message})

    if current_conversation:
        current_conversation['note_count'] = len(current_conversation.get('notes', []))
        current_conversation['reviewers'] = ', '.join(reviewers)
        conversation_data.append(current_conversation)

    return conversation_data


def filter_and_sort_notes(reviewer_username, reviewed_username, notes):
    conversation = []
    if all(note['author']['username'] == reviewed_username for note in notes):
        return conversation
    for note in notes:
        if not reviewer_username or note['author']['username'] == reviewer_username:
            if not note['system']:  # Filter out system notes
                conversation.append(note)
    return sorted(conversation, key=itemgetter('created_at'))


def get_notes(conversation):
    notes = []
    for note in conversation:
        notes.append({
            'created_at': convert_time_format(note['created_at']),
            'reviewer': note['author']['username'],
            'body': note['body']
        })
    return notes


def collect_conversation_data(conversation):
    first_note_date = convert_time_format(conversation[0]['created_at'])
    reviewers = {note['author']['username'] for note in conversation}
    return {
        'conversation_link': f"#note_{conversation[0]['id']}",
        'first_note_date': first_note_date,
        'note_count': len(conversation),
        'reviewers': ', '.join(reviewers)
    }


def main():
    parser = argparse.ArgumentParser(description='Fetch GitLab merge request comments.')
    parser.add_argument('--reviewed_username', help='Username of the user whose merge requests are to be reviewed')
    parser.add_argument('--reviewer_username', help='Username of the reviewer (optional)')
    offline_online_mode = parser.add_mutually_exclusive_group()
    offline_online_mode.add_argument('--gitlab_access_token')
    offline_online_mode.add_argument('--gitlab_previous_output')
    parser.add_argument('--analyze', default=True)
    parser.add_argument('--result_csv_file', default=f'gpt_analyzed_gitlab_comments_{uuid.uuid4()}.csv')
    parser.add_argument('--raw_json_file', default=f'raw_json_file_gitlab_comments_{uuid.uuid4()}.json')

    args = parser.parse_args()

    # Set default reviewed_username if both arguments are missing
    if not args.reviewed_username and not args.reviewer_username:
        args.reviewed_username = config.REVIEWED_USERNAME

    if args.gitlab_previous_output:
        # conversation_data = parse_gitlab_previous_output(args.gitlab_previous_output)
        conversation_data = load_json_to_dict(args.gitlab_previous_output)
    else:
        conversation_data = extract_gitlab_conversations(args)
        if args.raw_json_file:
            save_dict_to_json(conversation_data, args.raw_json_file)

    if args.analyze:
        print_analyze(conversation_data, args.result_csv_file)

    else:
        print(f"\n{'MR IID':<75} {'First Note Date':<28} {'Note Count':<3} {'Reviewers'}")
        for data in conversation_data:
            print(f"{data['conversation_link']:<75} {data['first_note_date']:<25} {data['note_count']:<3} {data['reviewers']}")


def get_line_and_type_from_position(first_note_position, direction):
    position_side = first_note_position['line_range'][direction]
    side_line_type = position_side['type']
    side_line = position_side['new_line'] if side_line_type == 'new' else position_side['old_line']
    return side_line, side_line_type


def print_analyze(conversation_data, result_csv_file):
    for data in conversation_data:
        try:
            conversation = '\n'.join(f"{n['reviewer']}: {n['body']}" for n in data['notes'])
            data['analyze'] = analyze_review_discussion(conversation, data['diff_text'], data['discussion_range'])
        except Exception as e:
            print(f'print_analyze error: "{e}"')
    print(f"\n{'MR IID':<75} {'First Note Date':<28} {'Note Count':<3} {'Analyze':<40} {'Reviewers'}")

    for data in conversation_data:
        print(
            f"{data['conversation_link']:<75} {data['first_note_date']:<25} {data['note_count']:<3} {data['analyze']:<40} {data['reviewers']}")
    if result_csv_file:
        print_to_csv(conversation_data, result_csv_file)


def print_to_csv(conversation_data, csv_file):
    with open(csv_file, mode='w', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)

        # Writing the header
        writer.writerow(['MR IID', 'First Note Date', 'Note Count', 'Analyze', 'Reviewers'])

        # Writing data rows
        for data in conversation_data:
            writer.writerow([
                data['conversation_link'],
                data['first_note_date'],
                data['note_count'],
                data['analyze'],
                data['reviewers']
            ])


def analyze_review_discussion(conversation, diff_text, discussion_range):
    """
    Send a request to the GPT API to analyze a review discussion.

    :return: The response from the GPT API.
    """
    # Endpoint for the GPT API
    endpoint = "https://api.openai.com/v1/engines/gpt-4/completions"

    # Prepare the prompt
    s_range = discussion_range['start']
    e_range = discussion_range['end']
    s_line, s_type = s_range['line'], s_range['type']
    e_line, e_type = e_range['line'], e_range['type']
    prompt = (f"Get me the insight from this code review discussion. up to 7 words:"
              f"\n\"\n{conversation}\n\".\n"
              f"The discussion refers to the code change introduced by the following diff - "
              f"the exact change is ranged between lines {s_line} ({s_type}) and {e_line} ({e_type}):"
              f"\n```\n{diff_text}\n```\n")
    # prompt = (f"I need you to analyze a code review discussion. "
    #           f"Get me the insight from this discussion. It can be 10-15 words in instruction format (\"if/when ... do ....\")."
    #           f"The Conversation: \n\"\n{conversation}\n\".\n"
    #           f"The discussion is references to the code change introduce by the following diff - "
    #           f"The discussion refers to the code change introduced by the following diff - "
    #           f"the exact change is ranged between lines {s_range['line']} ({s_range['type']}) "
    #           f"and {e_range['line']} ({e_range['type']}):\n```\n{diff_text}\n```\n")
    print(f'{prompt=}')

    messages = [{"role": "user", "content": prompt}]

    # completion = openai.chat.completions.create(model="gpt-3.5-turbo", max_tokens=105, temperature=0.7, messages=messages)

    client = OpenAI(api_key=config.OPENAI_API_KEY)

    @retry(wait=wait_random_exponential(min=1, max=60), stop=stop_after_attempt(6))
    def completion_with_backoff(**kwargs):
        return client.chat.completions.create(**kwargs)

    response = completion_with_backoff(model="gpt-4", max_tokens=105, temperature=0.7, messages=messages)
    answer = response.choices[0].message.content
    print(f'{answer=}')
    return answer
    # return completion.choices[0].message.content


def get_git_diff(base_sha, head_sha, path):
    """
    Executes a git diff command and returns the diff output.

    :param base_sha: The base SHA for the diff.
    :param head_sha: The head SHA for the diff.
    :param path: The path to the file or directory to diff.
    :return: The output of the git diff command.
    """
    command = ["git", "diff", "--color=never", f"{base_sha}..{head_sha}", "--", path]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        diff_lines = result.stdout.splitlines()

        # Skip meta-information lines at the beginning of the diff output
        start_line = 0
        for line in diff_lines:
            if line.startswith("@@"):
                break
            start_line += 1

        # Return the diff output from the first @@ line
        return '\n'.join(diff_lines[start_line:])
    except subprocess.CalledProcessError as e:
        print(f"Error occurred: {e}")
        print(f"Standard Output: {e.stdout}")
        print(f"Error Output: {e.stderr}")
        return None


def extract_line_from_diff(diff_text, line_number, line_type='new'):
    """
    Extracts a specific line from a git diff.

    :param diff_text: The full text of the git diff.
    :param line_number: The line number to extract.
    :param line_type: 'new' for the new version of the line, 'old' for the old version.
    :return: The text of the specified line, or None if not found.
    """
    lines = diff_text.split('\n')
    current_line_number_old, current_line_number_new = None, None

    for line in lines:
        if line.startswith('@@'):
            # Example format: @@ -12965,7 +12965,8 @@
            parts = line.split(' ')
            old_line_info, new_line_info = parts[1], parts[2]
            current_line_number_old = int(old_line_info.split(',')[0][1:])
            current_line_number_new = int(new_line_info.split(',')[0][1:])
        else:
            if line_type == 'new':
                if line.startswith('+'):
                    current_line_number_new += 1
                elif not line.startswith('-'):
                    current_line_number_new += 1
                    current_line_number_old += 1

                if current_line_number_new == line_number:
                    return line[1:].strip()  # Strip the '+' and any leading/trailing whitespace

            elif line_type == 'old':
                if line.startswith('-'):
                    current_line_number_old += 1
                elif not line.startswith('+'):
                    current_line_number_old += 1
                    current_line_number_new += 1

                if current_line_number_old == line_number:
                    return line[1:].strip()  # Strip the '-' and any leading/trailing whitespace

    return None

if __name__ == "__main__":
    main()
