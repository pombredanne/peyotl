from sh import git
import traceback
import sh
import re
import os
import locket
import codecs
from peyotl.phylesystem import get_HEAD_SHA1
from peyotl import get_logger
import shutil
import json
import hashlib
from peyotl.nexson_syntax import write_as_json
import tempfile #@TEMPORARY for deprecated write_study
_LOG = get_logger(__name__)
class MergeException(Exception):
    pass


class GitWorkflowError(Exception):
    def __init__(self, msg):
        self.msg = msg
    def __str__(self):
        return self.msg

def get_user_author(auth_info):
    '''Return commit author info from a dict. Returns username and author string.

    auth_info should have 3 strings:
        `login` (a github log in)
        `name`
        `email`
    username will be in the `login` value. It is used for WIP branch naming.
    the author string will be the name and email joined. This is used in commit messages.
    '''
    return auth_info['login'], ("%s <%s>" % (auth_info['name'], auth_info['email']))

class GitAction(object):
    def __init__(self, repo, remote=None, git_ssh=None, pkey=None):
        """Create a GitAction object to interact with a Git repository

        Example:
        gd   = GitAction(repo="/home/user/git/foo")

        Note that this requires write access to the
        git repository directory, so it can create a
        lockfile in the .git directory.

        """
        self.repo = repo
        self.git_dir = os.path.join(repo, '.git')
        self.lock_file = os.path.join(self.git_dir, "API_WRITE_LOCK")
        self.lock_timeout = 30
        self.lock = locket.lock_file(self.lock_file, timeout=self.lock_timeout)
        self.repo_remote = remote
        self.git_ssh = git_ssh
        self.pkey = pkey
        
        if os.path.isdir("{}/.git".format(self.repo)):
            self.gitdir = "--git-dir={}/.git".format(self.repo)
            self.gitwd = "--work-tree={}".format(self.repo)
        else: #EJM needs a test?
            raise ValueError('Repo "{repo}" is not a git repo'.format(repo=self.repo))
    def paths_for_study(self, study_id):
        '''Returns study_dir and study_filepath for study_id.
        '''
        study_dir = "{r}/study/{id}".format(r=self.repo, id=study_id) #TODO change directory
        study_filename = "{d}/{id}.json".format(d=study_dir, id=study_id)
        return study_dir, study_filename

    def md5_for_study(self, study_id):
        fd, fp = self.paths_for_study(study_id)
        with open(fp, 'rb') as fo:
            study_md5 = md5_for_file(fo)
        return study_md5

    def env(self):
        return {'GIT_SSH': self.git_ssh,
                'PKEY': self.pkey,
                }

    def acquire_lock(self):
        "Acquire a lock on the git repository"
        _LOG.debug('Acquiring lock')
        self.lock.acquire()

    def release_lock(self):
        "Release a lock on the git repository"
        _LOG.debug('Releasing lock')
        try:
            self.lock.release()
        except:
            _LOG.debug('Exception releasing lock suppressed.')
            pass

    def current_branch(self):
        "Return the current branch name"
        branch_name = git(self.gitdir, self.gitwd, "symbolic-ref", "HEAD")
        return branch_name.replace('refs/heads/', '').strip()

    def checkout_master(self):
        git(self.gitdir, self.gitwd, "checkout", "master")

    def get_master_sha(self):
        x = git(self.gitdir, self.gitwd, "show-ref", "master", "--heads", "--hash")
        return x.strip()

    def newest_study_id(self):
        "Return the numeric part of the newest study_id"
        git(self.gitdir, self.gitwd, "checkout", "master")
        dirs = []
        # first we look for studies already in our master branch
        _study_dir = os.path.join(self.repo, "study")
        for f in os.listdir(_study_dir):
            if os.path.isdir(os.path.join(_study_dir, f)):
                # ignore alphabetic prefix, o = created by opentree API
                if f[0].isalpha():
                    dirs.append(int(f[1:]))
                else:
                    dirs.append(int(f))

        # next we must look at local branch names for new studies
        # without --no-color we get terminal color codes in the branch output
        branches = git(self.gitdir, self.gitwd, "branch", "--no-color")
        branches = [ b.strip() for b in branches ]
        for b in branches:
            mo = re.match(".+_o(\d+)", b)
            if mo:
                dirs.append(int(mo.group(1)))
        dirs.sort()
        return dirs[-1]

    def return_study(self, study_id, branch='master', return_WIP_map=False): 
        """Return the 
            blob[0] contents of the given study_id, 
            blob[1] the SHA1 of the HEAD.
            blob[2] dictionary of WIPs for this study.
        If the study_id does not exist, it returns the empty string.
        """
        self.checkout(branch)
        study_filename = self.paths_for_study(study_id)[1]
        head_sha = get_HEAD_SHA1(self.git_dir)
        
        try:
            f = codecs.open(study_filename, mode='rU', encoding='utf-8')
            content = f.read()
        except:
            content = ''
        if return_WIP_map:
            d = self.find_WIP_branches(study_id)
            return content, head_sha, d
        return content, head_sha

    def branch_exists(self, branch):
        """Returns true or false depending on if a branch exists"""
        try:
            git(self.gitdir, self.gitwd, "rev-parse", branch)
        except sh.ErrorReturnCode:
            return False
        return True

    def find_WIP_branches(self, study_id):
        pat = re.compile(r'.*_study_{i}_[0-9]+'.format(i=study_id))
        head_shas = git(self.gitdir, self.gitwd, "show-ref", "--heads")
        ret = {}
        _LOG.debug('find_WIP_branches head_shas = "{}"'.format(head_shas.split('\n')))
        for lin in head_shas.split('\n'):
            try:
                local_branch_split = lin.split(' refs/heads/')
                if len(local_branch_split) == 2:
                    sha, branch = local_branch_split
                    if pat.match(branch) or branch == 'master':
                        ret[branch] = sha
            except:
                raise
        return ret

    def _find_head_sha(self, frag, parent_sha):
        head_shas = git(self.gitdir, self.gitwd, "show-ref", "--heads")
        for lin in head_shas.split('\n'):
            #_LOG.debug("lin = '{l}'".format(l=lin))
            if lin.startswith(parent_sha):
                local_branch_split = lin.split(' refs/heads/')
                #_LOG.debug("local_branch_split = '{l}'".format(l=local_branch_split))
                if len(local_branch_split) == 2:
                    branch = local_branch_split[1].rstrip()
                    if branch.startswith(frag):
                         return branch
        return None
    def checkout(self, branch):
        git(self.gitdir, self.gitwd, "checkout", branch)

    def create_or_checkout_branch(self, gh_user, study_id, parent_sha, force_branch_name=False):
        if force_branch_name:
            #@TEMP deprecated
            branch = "{ghu}_study_{rid}".format(ghu=gh_user, rid=study_id)
            if not self.branch_exists(branch):
                try:
                    git(self.gitdir, self.gitwd, "branch", branch, parent_sha)
                    _LOG.debug('Created branch "{b}" with parent "{a}"'.format(b=branch, a=parent_sha))
                except:
                    raise ValueError('parent sha not in git repo')
            self.checkout(branch)
            return branch

        frag = "{ghu}_study_{rid}_".format(ghu=gh_user, rid=study_id)
        branch = self._find_head_sha(frag, parent_sha)
        _LOG.debug('Found branch "{b}" for sha "{s}"'.format(b=branch, s=parent_sha))
        if not branch:
            branch = frag + '0'
            i=1
            while self.branch_exists(branch):
                branch = frag + str(i)
                i+=1
            _LOG.debug('lowest non existing branch =' + branch)
            try:
                git(self.gitdir, self.gitwd, "branch", branch, parent_sha)
                _LOG.debug('Created branch "{b}" with parent "{a}"'.format(b=branch, a=parent_sha))
            except:
                raise ValueError('parent sha not in git repo')
        self.checkout(branch)
        _LOG.debug('Checked out branch "{b}"'.format(b=branch))
        return branch


    def remove_study(self, gh_user, study_id, parent_sha, author="OpenTree API <api@opentreeoflife.org>"):
        """Remove a study
        Given a study_id, branch and optionally an
        author, remove a study on the given branch
        and attribute the commit to author.
        Returns the SHA of the commit on branch.
        """
        study_dir, study_filename = self.paths_for_study(study_id)

        branch = self.create_or_checkout_branch(gh_user, study_id, parent_sha)
        if not os.path.isdir(study_dir):
            # branch already exists locally with study removed
            # so just return the commit SHA
            return git(self.gitdir, self.gitwd, "rev-parse", "HEAD").strip(), branch
        git(self.gitdir, self.gitwd, "rm", "-rf", study_dir)
        git(self.gitdir, self.gitwd, "commit", author=author, message="Delete Study #%s via OpenTree API" % study_id)
        new_sha = git(self.gitdir, self.gitwd, "rev-parse", "HEAD")
        return new_sha.strip(), branch
        

    def reset_hard(self):
        try:
            git(self.gitdir, self.gitwd, 'reset', '--hard')
        except:
            _LOG.exception('"git reset --hard" failed.')

    def get_blob_sha_for_file(self, filepath, branch='HEAD'):
        try:
            r = git(self.gitdir, self.gitwd, 'ls-tree', branch, filepath)
            #_LOG.debug('ls-tree said "{}"'.format(r))
            line = r.strip()
            ls = line.split()
            #_LOG.debug('ls is "{}"'.format(str(ls)))
            assert(len(ls) == 4)
            assert(ls[1] == 'blob')
            return ls[2]
        except:
            _LOG.exception('git ls-tree failed')
            raise

    #@TEMP TODO: remove this form...
    def write_study(self, study_id, file_content, branch, author):
        """Given a study_id, temporary filename of content, branch and auth_info
        
        Deprecated but needed until we merge api local-dep to master...

        """
        parent_sha = None
        #@TODO. DANGER super-ugly hack to get gh_user 
        #   only doing this function is going away very soon.
        gh_user = branch.split('_study_')[0]
        fc = tempfile.NamedTemporaryFile()
        if isinstance(file_content, str) or isinstance(file_content, unicode):
            fc.write(file_content)
        else:
            write_as_json(file_content, fc)
        fc.flush()
        try:
            study_dir, study_filename = self.paths_for_study(study_id) 
            if parent_sha is None:
                self.checkout_master()
                parent_sha = self.get_master_sha()
            branch = self.create_or_checkout_branch(gh_user, study_id, parent_sha, force_branch_name=True)
            # create a study directory if this is a new study EJM- what if it isn't?
            if not os.path.isdir(study_dir):
                os.mkdir(study_dir)
            shutil.copy(fc.name, study_filename)
            git(self.gitdir, self.gitwd, "add", study_filename)
            try:
                git(self.gitdir, self.gitwd,  "commit", author=author, message="Update Study #%s via OpenTree API" % study_id)
            except Exception, e:
                # We can ignore this if no changes are new,
                # otherwise raise a 400
                if "nothing to commit" in e.message:#@EJM is this dangerous?
                     pass
                else:
                    _LOG.exception('"git commit" failed')
                    self.reset_hard()
                    raise
            new_sha = git(self.gitdir, self.gitwd,  "rev-parse", "HEAD")
        except Exception, e:
            _LOG.exception('write_study exception')
            raise GitWorkflowError("Could not write to study #%s ! Details: \n%s" % (study_id, e.message))
        finally:
            fc.close()
        return new_sha


    def write_study_from_tmpfile(self, study_id, tmpfi, parent_sha, auth_info):
        """Given a study_id, temporary filename of content, branch and auth_info
        """
        gh_user, author = get_user_author(auth_info)
        study_dir, study_filename = self.paths_for_study(study_id) 
        if parent_sha is None:
            self.checkout_master()
            parent_sha = self.get_master_sha()
        branch = self.create_or_checkout_branch(gh_user, study_id, parent_sha)
        
        # create a study directory if this is a new study EJM- what if it isn't?
        if not os.path.isdir(study_dir):
            os.mkdir(study_dir)
        
        if os.path.exists(study_filename):
            prev_file_sha = self.get_blob_sha_for_file(study_filename)
        else:
            prev_file_sha = None
        shutil.copy(tmpfi.name, study_filename)
        git(self.gitdir, self.gitwd, "add", study_filename)
        try:
            git(self.gitdir, self.gitwd,  "commit", author=author, message="Update Study #%s via OpenTree API" % study_id)
        except Exception, e:
            # We can ignore this if no changes are new,
            # otherwise raise a 400
            if "nothing to commit" in e.message:#@EJM is this dangerous?
                 pass
            else:
                _LOG.exception('"git commit" failed')
                self.reset_hard()
                raise
        new_sha = git(self.gitdir, self.gitwd,  "rev-parse", "HEAD")
        _LOG.debug('Committed study "{i}" to branch "{b}" commit SHA: "{s}"'.format(i=study_id, b=branch, s=new_sha.strip()))
        return {'commit_sha': new_sha.strip(),
                'branch': branch,
                'prev_file_sha': prev_file_sha,
               }

    def merge(self, branch, destination="master"):
        """
        Merge the the given WIP branch to master (or destination, if specified)

        If the merge fails, the merge will be aborted
        and then a MergeException will be thrown. The
        message of the MergeException will be the
        "git status" output, so details about merge
        conflicts can be determined.
        """
        current_branch = self.current_branch()
        if current_branch != destination:
            _LOG.debug('checking out ' + destination)
            git(self.gitdir, self.gitwd, "checkout", destination)
        try:
            git(self.gitdir, self.gitwd, "merge", branch)
        except sh.ErrorReturnCode:
            _LOG.exception('merge failed')
            # attempt to reset things so other operations can continue
            git(self.gitdir, self.gitwd, "merge", "--abort")
            # raise an MergeException so that caller will know that the merge failed
            raise MergeException()

        new_sha = git(self.gitdir, self.gitwd, "rev-parse", "HEAD")
        return new_sha.strip()

    def delete_branch(self, branch):
        git(self.gitdir, self.gitwd, 'branch', '-d', branch)