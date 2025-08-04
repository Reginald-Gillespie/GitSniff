#!/usr/bin/env python3

import argparse
import json
import requests
import sys
import os
import tempfile
import shutil
import subprocess
import re
import stat
import signal
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn
from rich.panel import Panel
from collections import defaultdict
from math import ceil


def on_rm_error(func, path, exc_info):
    # Change the file to be writable and try again
    os.chmod(path, stat.S_IWRITE)
    try:
        func(path)
    except Exception as e:
        console.print(f"[dim]Still couldn't delete {path}: {e}[/dim]")

console = Console()

class GitHubScanner:
    def __init__(self, token=None, proxy=None):
        self.proxies = {"https": proxy or ""}
        self.headers = {"Authorization": f"token {token}"} if token else {}
        self.base_url = "https://api.github.com/"
        self.results = []
        self.interrupted = False
        self.output_interrupted = False
        self.unique_emails = set()
        
        # Print results when stopped
        signal.signal(signal.SIGINT, self._signal_handler)
        
    def _signal_handler(self, signum, frame):
        """Handle Ctrl+C interruption"""
        if self.output_interrupted:
            console.print("\n[red]Force exit requested[/red]")
            sys.exit(1)
        elif self.interrupted:
            self.output_interrupted = True
            console.print("\n[yellow]Force exit on next interrupt...[/yellow]")
        else:
            self.interrupted = True
            console.print("\n[yellow]Interrupt received, finishing current operation...[/yellow]")
        
    def _make_request(self, url, description="Making request"):
        """Make a request with error handling"""
        try:
            response = requests.get(url, proxies=self.proxies, headers=self.headers)
            
            if response.status_code == 403:
                data = response.json()
                if "rate limit exceeded" in data.get("message", "").lower():
                    console.print("[red]Rate limit exceeded! Please provide a token or wait.[/red]")
                    sys.exit(1)
                    
            if response.status_code == 404:
                console.print(f"[dim]{description}: Not found[/dim]")
                return None
                
            if response.status_code != 200:
                console.print(f"[red]{description} failed: {response.json().get('message', 'Unknown error')}[/red]")
                return None
                
            return response.json()
        except requests.exceptions.RequestException as e:
            console.print(f"[red]Network error: {e}[/red]")
            return None
    
    def _check_git_installed(self):
        """Check if git is installed and available"""
        try:
            result = subprocess.run(['git', '--version'], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        
        console.print("[red]Git is not installed or not available in PATH[/red]")
        console.print("[yellow]Please install git to use this tool for repository scanning[/yellow]")
        return False

    def _clone_repository(self, repo_url, temp_dir):
        """Clone a repository to a temporary directory"""
        try:
            result = subprocess.run([
                'git', 'clone', '--depth=1000', '--bare', repo_url, temp_dir
            ], capture_output=True, text=True, timeout=300)
            
            if result.returncode != 0:
                console.print(f"[dim]Failed to clone {repo_url}: {result.stderr.strip()}[/dim]")
                return False
            
            return True
        except subprocess.TimeoutExpired:
            console.print(f"[dim]Timeout cloning {repo_url}[/dim]")
            return False
        except Exception as e:
            console.print(f"[dim]Error cloning {repo_url}: {e}[/dim]")
            return False
    
    def _extract_emails_from_git_log(self, repo_path, repo_name, max_commits=1000):
        """Extract emails from git log in the cloned repository"""
        emails = []
        try:
            result = subprocess.run([
                'git', '--git-dir', repo_path, 'log', 
                f'--max-count={max_commits}',
                '--pretty=format:%H|%an|%ae|%cn|%ce',
                '--all'
            ], capture_output=True, text=True, timeout=60)
            
            if result.returncode != 0:
                console.print(f"[dim]Failed to get git log for {repo_name}: {result.stderr.strip()}[/dim]")
                return emails
            
            new_emails_found = []
            
            for line in result.stdout.strip().split('\n'):
                if not line: continue
                parts = line.split('|')
                if len(parts) != 5: continue
                commit_sha, author_name, author_email, committer_name, committer_email = parts
                
                for name, email in [(author_name, author_email), (committer_name, committer_email)]:
                    self._process_email(email, name, f"https://github.com/{repo_name}", commit_sha, emails, new_emails_found)

            if new_emails_found:
                console.print(f"[blue]New emails in {repo_name.split('/')[-1]}: [green]{', '.join(new_emails_found)}[/green][/blue]")
        
        except subprocess.TimeoutExpired:
            console.print(f"[dim]Timeout reading git log for {repo_name}[/dim]")
        except Exception as e:
            console.print(f"[dim]Error reading git log for {repo_name}: {e}[/dim]")
        
        return emails
    
    def _extract_emails_from_commits(self, repo_name, max_commits=1000, task_id=None, progress=None):
        """Extract emails from repository commits by cloning locally"""
        emails = []
        if progress and task_id:
            progress.update(task_id, description=f"Cloning {repo_name}")
        
        temp_dir = tempfile.mkdtemp(prefix="github_scanner_")
        try:
            repo_url = f"https://github.com/{repo_name}.git"
            if self._clone_repository(repo_url, temp_dir):
                if progress and task_id:
                    progress.update(task_id, description=f"Scanning {repo_name}")
                emails = self._extract_emails_from_git_log(temp_dir, repo_name, max_commits)
            else:
                if progress and task_id:
                    progress.update(task_id, description=f"Failed to clone {repo_name}")
        finally:
            shutil.rmtree(temp_dir, onerror=on_rm_error)
        
        return emails
    
    def _scan_repos(self, username, all_repos, max_commits):
        """The core logic for scanning a list of repositories."""
        all_emails = []
        
        
        with Progress(
            SpinnerColumn(), 
            TextColumn(
                "[progress.description]{task.description:<60}", 
                style="white",
            ),
            BarColumn(), 
            TaskProgressColumn(), 
            TimeRemainingColumn(), 
            console=console
        ) as progress:
            
            overall_task = progress.add_task(f"Scanning {username}'s repositories", total=len(all_repos))
            repo_task = progress.add_task("Preparing...", total=None)
            
            for i, repo in enumerate(all_repos):
                if self.interrupted:
                    console.print(f"\n[yellow]Repo scan interrupted after {i} repositories[/yellow]")
                    break
                    
                repo_name = repo["full_name"]
                progress.update(
                    overall_task, completed=i,
                    description=f"Scanning Repos ({i+1}/{len(all_repos)}) - {len(self.unique_emails)} unique emails"
                )
                progress.reset(repo_task, total=None)
                
                repo_emails = self._extract_emails_from_commits(repo_name, max_commits, repo_task, progress)
                all_emails.extend(repo_emails)
                
                progress.update(repo_task, description=f"{repo_name} - Found {len(repo_emails)} emails")
            
            progress.update(overall_task, completed=min(i+1 if 'i' in locals() else len(all_repos), len(all_repos)))
            
        return all_emails

    def _scan_events(self, username, max_events=1000):
        """Scan public events for a user to find emails in PushEvents."""
        console.print(Panel.fit(f"Stage: [bold]Event Scan[/bold]\nScanning up to [cyan]{max_events}[/cyan] public events for [yellow]{username}[/yellow].", border_style="yellow"))
        
        all_emails = []
        page = 1
        events_scanned = 0
        pages_to_scan = ceil(max_events / 100)

        with Progress(
            SpinnerColumn(), 
            TextColumn("[progress.description]{task.description}", style="white"),
            BarColumn(), 
            TaskProgressColumn(), 
            TimeRemainingColumn(), 
            console=console
        ) as progress:
            
            task = progress.add_task(f"Scanning {username}'s events", total=pages_to_scan)

            while events_scanned < max_events:
                if self.interrupted:
                    console.print("[yellow]Interrupted while scanning events[/yellow]")
                    break

                events_url = f"{self.base_url}users/{username}/events?per_page=100&page={page}"
                progress.update(task, description=f"Fetching events page {page}/{pages_to_scan}")
                events_data = self._make_request(events_url, f"Getting events for {username} (page {page})")

                if not events_data:
                    console.print("[dim]No more events found.[/dim]")
                    break

                new_emails_found = []
                for event in events_data:
                    if events_scanned >= max_events: break
                    events_scanned += 1
                    
                    if event.get('type') == 'PushEvent':
                        commits = event.get('payload', {}).get('commits', [])
                        repo_name = event.get('repo', {}).get('name', 'N/A')
                        
                        for commit in commits:
                            author = commit.get('author', {})
                            email = author.get('email')
                            name = author.get('name')
                            commit_sha = commit.get('sha')
                            
                            self._process_email(email, name, f"https://github.com/{repo_name}", commit_sha, all_emails, new_emails_found)

                if new_emails_found:
                    console.print(f"[blue]📧 New emails from events: [green]{', '.join(new_emails_found)}[/green][/blue]")

                progress.update(task, advance=1)
                page += 1
                if len(events_data) < 100: break # Last page
            
            # Mark progress bar as complete
            progress.update(task, completed=pages_to_scan)
        
        console.print(f"[blue]Event scan complete! Found {len(all_emails)} email entries from events.[/blue]")
        return all_emails

    def _process_email(self, email, name, repo_url, commit_sha, email_list, new_email_tracker):
        """Helper to process, filter, and store a found email."""
        if not email: return
        
        email = email.lower().strip()
        name = name.strip() if name else ""

        if "users.noreply.github.com" in email or email == "noreply@github.com":
            return

        email_data = {
            "email": email, "name": name, "repo": repo_url, "commit_sha": commit_sha
        }
        email_list.append(email_data)
        
        if email not in self.unique_emails:
            self.unique_emails.add(email)
            new_email_tracker.append(email)

    def scan_repository(self, repo_name, max_commits=1000):
        """Scan a single repository for emails"""
        console.print(f"\n[blue]Scanning repository: {repo_name}[/blue]")
        if not self._check_git_installed(): return []
        
        repo_data = self._make_request(f"{self.base_url}repos/{repo_name}", f"Getting repo info for {repo_name}")
        if not repo_data or repo_data.get("size", 0) == 0:
            console.print(f"[yellow]Repository {repo_name} not found or is empty[/yellow]")
            return []
        
        with Progress(
            SpinnerColumn(), 
            TextColumn("[progress.description]{task.description}"), 
            console=console,
            transient=True
        ) as progress:
            task = progress.add_task(f"Scanning {repo_name}", total=None)
            emails = self._extract_emails_from_commits(repo_name, max_commits, task, progress)
        
        console.print(f"[blue]Found {len(emails)} email entries in {repo_name}[/blue]")
        return emails
    
    def scan_user_or_org(self, username, include_forks=False, include_private=False, 
                         max_commits=1000, start_index=0, max_repos=None,
                         scan_repos=True, scan_events=True, max_events=1000):
        """Scan a user/org via repositories and/or public events."""
        user_data = self._make_request(f"{self.base_url}users/{username}", f"Getting info for {username}")
        if not user_data: return []

        user_type = user_data.get("type", "user")
        console.print(f"[blue]Scanning {user_type}: {username} ({user_data.get('public_repos', 0)} public repos)[/blue]")
        
        all_emails = []

        if scan_repos:
            console.print(Panel.fit("Stage: [bold]Repository Scan[/bold]\nCloning repos to inspect commit history.", border_style="yellow"))
            if not self._check_git_installed():
                console.print("[red]Skipping repository scan.[/red]")
            else:
                console.print("[dim]Fetching repository list...[/dim]")
                all_repos = []
                page = 1
                while True:
                    if self.interrupted: break
                    repo_url = f"{self.base_url}{'orgs' if user_type == 'Organization' else 'users'}/{username}/repos?per_page=100&page={page}"
                    if include_private and self.headers: repo_url += "&type=all"
                    
                    repos_data = self._make_request(repo_url, f"Getting repos (page {page})")
                    if not repos_data: break
                    
                    for repo in repos_data:
                        if (not include_forks and repo.get("fork")) or repo.get("size", 0) == 0:
                            continue
                        if repo.get("private") and not include_private:
                            continue
                        all_repos.append(repo)
                    
                    if len(repos_data) < 100: break
                    page += 1

                if not all_repos:
                    console.print("[yellow]No scannable repositories found.[/yellow]")
                else:
                    if start_index > 0: all_repos = all_repos[start_index:]
                    if max_repos is not None: all_repos = all_repos[:max_repos]
                    
                    console.print(f"[dim]Found {len(all_repos)} repositories to scan.[/dim]")
                    repo_emails = self._scan_repos(username, all_repos, max_commits)
                    all_emails.extend(repo_emails)
                    console.print(f"[blue]Repository scan complete! Found {len(repo_emails)} email entries.[/blue]")

        if self.interrupted:
            console.print("[yellow]Scan interrupted before event scan.[/yellow]")
            return all_emails

        if scan_events:
            event_emails = self._scan_events(username, max_events)
            all_emails.extend(event_emails)

        console.print()
        console.print(f"[bold blue]Full scan complete! Found {len(all_emails)} total email entries.[/bold blue]")
        console.print(f"[bold blue]Total unique emails discovered: {len(self.unique_emails)}[/bold blue]")
        return all_emails

    def _organize_results(self, results):
        """Organize results by email, showing all repos and commits for each email"""
        organized = defaultdict(lambda: {'name': '', 'repos': defaultdict(list)})
        for result in results:
            email = result['email']
            repo = result['repo']
            commit = result['commit_sha']
            if result.get('name') and not organized[email]['name']:
                organized[email]['name'] = result['name']
            organized[email]['repos'][repo].append(commit)
        return organized
    
    def display_results(self, results, output_format="table"):
        """Display results in a formatted way"""
        if not results:
            console.print("\n[yellow]No emails found in the scan.[/yellow]")
            return
        
        self.output_interrupted = False
        
        if output_format == "json":
            try:
                organized = self._organize_results(results)
                json_output = {
                    email: {
                        'name': data['name'],
                        'repositories': {
                            repo: {'commits': commits, 'commit_count': len(commits)}
                            for repo, commits in data['repos'].items()
                        }
                    } for email, data in organized.items()
                }
                console.print(json.dumps(json_output, indent=2))
            except KeyboardInterrupt:
                if not self.output_interrupted:
                    console.print("\n[yellow]Output interrupted[/yellow]")
                    self.output_interrupted = True
            return

        organized = self._organize_results(results)
        table = Table(title="🔍 GitHub Email Scan Results")
        table.add_column("Email", style="cyan", no_wrap=True)
        table.add_column("Name", style="green")
        table.add_column("Source Repository", style="blue")
        table.add_column("Commits", style="magenta")
        
        try:
            for email in sorted(organized.keys()):
                data = organized[email]
                repos_list = list(data['repos'].items())
                for i, (repo, commits) in enumerate(repos_list):
                    repo_name = repo.replace("https://github.com/", "")
                    commit_text = f"{commits[0][:8]} (and {len(commits) - 1} more)" if len(commits) > 1 else (commits[0][:8] if commits else "")
                    
                    table.add_row(
                        email if i == 0 else "",
                        data['name'] if i == 0 else "",
                        repo_name,
                        commit_text
                    )
            console.print("\n"); console.print(table)
        except KeyboardInterrupt:
            if not self.output_interrupted:
                console.print("\n[yellow]Output interrupted[/yellow]")
                self.output_interrupted = True
        
        try:
            summary = Panel.fit(
                f"📊 [bold]Summary[/bold]\n"
                f"• Unique emails: {len(organized)}\n"
                f"• Repositories with findings: {len(set(r['repo'] for r in results))}\n"
                f"• Total commit entries found: {len(results)}",
                title="Scan Summary", border_style="green"
            )
            console.print(summary)
        except KeyboardInterrupt:
            if not self.output_interrupted:
                console.print("\n[yellow]Output interrupted[/yellow]")
                self.output_interrupted = True

def load_token_from_file(token_path="pat.env"):
    """Load token from file, supporting both raw token and token=value format"""
    try:
        if os.path.exists(token_path):
            with open(token_path, 'r') as f: content = f.read().strip()
            if '=' in content:
                for line in content.split('\n'):
                    if line.strip().startswith('token='): return line.split('=', 1)[1].strip()
            else:
                return content
    except Exception as e:
        console.print(f"[yellow]Warning: Could not read token file {token_path}: {e}[/yellow]")
    return None

epilog ="""
Examples:
  %(prog)s -u username                     # Scan user's repositories and events
  %(prog)s -u username --no-events         # Scan only repositories
  %(prog)s -u username --no-repos          # Scan only public events
  %(prog)s -r owner/repo                   # Scan specific repository
  %(prog)s -u username -f --max-repos 5    # Include forks, limit to 5 repos
  %(prog)s -u username --private -t TOKEN  # Include private repos with token
"""

def main():
    parser = argparse.ArgumentParser(
        description="GitSniff - GitHub Email Scanner for OSINT",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
        add_help=False
    )
    
    # Custom help to make it cleaner
    parser.add_argument('-h', '--help', action='help', help='Show this help message and exit')
    
    # Target selection (required)
    target_group = parser.add_argument_group('Target Selection (required)')
    target_mutex = target_group.add_mutually_exclusive_group(required=True)
    target_mutex.add_argument("-u", "--user", metavar="USERNAME", help="Scan GitHub user or organization")
    target_mutex.add_argument("-r", "--repo", metavar="OWNER/REPO", help="Scan single repository")
    
    # Scanning methods
    scan_group = parser.add_argument_group('Scanning Methods')
    scan_group.add_argument("--no-repos", action="store_true", help="Disable repository cloning scan")
    scan_group.add_argument("--no-events", action="store_true", help="Disable public event API scan")

    # Repository options
    repo_group = parser.add_argument_group('Repository Options')
    repo_group.add_argument("-f", "--forks", action="store_true", help="Include forked repositories")
    repo_group.add_argument("--private", action="store_true", help="Include private repositories (requires token)")
    repo_group.add_argument("--max-commits", type=int, default=1000, metavar="N", help="Max commits per repository (default: 1000)")
    repo_group.add_argument("--start-index", type=int, default=0, metavar="N", help="Start repo scan from index N (default: 0)")
    repo_group.add_argument("--max-repos", type=int, metavar="N", help="Maximum repositories to scan")
    
    # Event options
    event_group = parser.add_argument_group('Event Options')
    event_group.add_argument("--max-events", type=int, default=1000, metavar="N", help="Max public events to scan (default: 1000)")

    # Connection and output
    conn_group = parser.add_argument_group('Connection & Output')
    conn_group.add_argument("-t", "--token", metavar="TOKEN", help="GitHub personal access token")
    conn_group.add_argument("--token-file", metavar="FILE", help="Path to token file (default: pat.env)")
    conn_group.add_argument("-p", "--proxy", metavar="URL", help="HTTP/SOCKS proxy URL")
    conn_group.add_argument("-j", "--json", action="store_true", help="Output in JSON format")
    
    args = parser.parse_args()
    
    token = args.token or load_token_from_file(args.token_file or "pat.env")
    if token and not args.token:
        console.print(f"[dim]Loaded token from {args.token_file or 'pat.env'}[/dim]")
    
    if args.private and not token:
        console.print("[red]--private flag requires a GitHub token.[/red]"); sys.exit(1)
    if args.user and args.no_repos and args.no_events:
        console.print("[red]Cannot use --no-repos and --no-events together for a user scan.[/red]"); sys.exit(1)
    if (args.start_index != 0 or args.max_repos is not None) and args.no_repos:
        console.print("[yellow]Repo filtering options like --start-index have no effect with --no-repos.[/yellow]")

    console.print(Panel.fit(
        "[bold cyan]GitSniff - GitHub Email Scanner[/bold cyan]\n"
        "[dim]Scanning via repository commits and public user events.[/dim]\n"
        "[dim]Press Ctrl+C once to break and show results, twice to force exit.[/dim]",
        border_style="cyan"
    ))
    
    scanner = GitHubScanner(token=token, proxy=args.proxy)
    
    try:
        results = []
        if args.user:
            results = scanner.scan_user_or_org(
                args.user, 
                include_forks=args.forks, 
                include_private=args.private, 
                max_commits=args.max_commits, 
                start_index=args.start_index, 
                max_repos=args.max_repos,
                scan_repos=not args.no_repos,
                scan_events=not args.no_events,
                max_events=args.max_events
            )
        elif args.repo:
            if args.no_repos:
                console.print("[red]Cannot use --no-repos when scanning a single repository.[/red]"); sys.exit(1)
            results = scanner.scan_repository(args.repo, args.max_commits)
        
        scanner.display_results(results, "json" if args.json else "table")
        
    except KeyboardInterrupt:
        console.print("\n[yellow]Scan interrupted by user.[/yellow]")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[red]An unexpected error occurred: {e}[/red]")
        sys.exit(1)

if __name__ == "__main__":
    main()