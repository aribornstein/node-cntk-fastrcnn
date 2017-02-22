const fs = require('fs');
const path = require('path');
const os = require('os');
const util = require('util');
const tmp = require('tmp');
const exec = require('child_process').exec;

const CNTK_CMD_TEMPLATE = "%s " + path.join(__dirname, 'frcnn_detector.py') + 
                          ' --input %s --json-output %s --model %s --cntk-path %s'

function getLastSortedDirectory(prefix, path) {
    var entries = fs.readdirSync(path)
    var filteredEntries = entries.filter((value) => {return value.toLowerCase().startsWith(prefix)});
    filteredEntries.sort();
    return filteredEntries[filteredEntries.length - 1];
}

function resolveCntkEnvDir(cntkInstallDir) {
    var anacondaPath = getLastSortedDirectory('anaconda3-', cntkInstallDir);
    var envsPath = path.join(cntkInstallDir, anacondaPath, 'envs');
    var cntkEnvDir = getLastSortedDirectory('cntk-py', envsPath);
    return path.join(envsPath, cntkEnvDir);
}

function getAndEnsureJsonTempDir() {
    var tmpDirPath = path.join(os.tmpDir(), 'node_cntk_fastrcnn');
    if (!fs.existsSync(tmpDirPath)) {
        fs.mkdirSync(tmpDirPath);
    }

    return tmpDirPath;
}

function buildCntkCmd(directoryPath, cntkModelPath, cntkEnvDirPath, jsonFilePath, cntkInstallDir) {
    return util.format(CNTK_CMD_TEMPLATE, path.join(cntkEnvDirPath, 'python'), directoryPath, 
                       jsonFilePath, cntkModelPath, cntkInstallDir)
}

function runCNTK(cntk_cmd, cb) {
    var proc = exec(cntk_cmd);
    
    var error_data = '';
    var output_data = '';

    proc.stdout.on('data', function(data) {
        output_data += data;
    });

    proc.stderr.on('data', function(data) {
        error_data += data;
    });

    proc.on('exit', (exitCode)=> {
        var err = null;
        if (exitCode != 0) {
            var errorMesage = util.format("CNTK Process failed with error code %d\nError output:%s",exitCode, error_data);
            err = Error(errorMesage)
        }
        cb(err, output_data);
    });
}

function evalDirectoryImp(directoryPath, cntkModelPath, cntkEnvDirPath, jsonTempDir, cntkInstallDir, verbose, cb) {
    tmp.tmpName({dir : jsonTempDir, postfix: '.json'}, (err, jsonFilePath)=> {
        if (err) {
            return cb(err);
        }
        
        var cmd = buildCntkCmd(directoryPath, cntkModelPath, cntkEnvDirPath, jsonFilePath, cntkInstallDir);
        runCNTK(cmd, (err, output_data) =>{
            console.info('verbose')
            if (verbose) {
                console.info('CNTK process output:')
                console.info(output_data);
            }

            if (err) {
                return cb(err);
            }
            fs.readFile(jsonFilePath, (err, content) =>{
                // call delete on the file and ignore the result since the json is in a temp directory anyway
                if (fs.existsSync(jsonFilePath)) {
                    fs.unlink(jsonFilePath);
                }

                if (err) {
                    return cb(err);
                }
                
                try {
                    return cb(null, JSON.parse(content));
                }
                catch (e) {
                    return cb(e);
                }
            })
        });
    });
}

function EvalClient(cntkModelPath, cntkInstallDir, verbose) {
    this.cntkInstallDir = cntkInstallDir;
    this.cntkModelPath = cntkModelPath;
    this.verbose = !!verbose;
    this.cntkEnvDirPath = resolveCntkEnvDir(cntkInstallDir);
    // add to path..
    process.env.PATH = this.cntkEnvDirPath + ';' + process.env.PATH;
    this.jsonTempDir = getAndEnsureJsonTempDir();

    this.evalDirectory = function(directoryPath, cb) {
        try {
            evalDirectoryImp(directoryPath, this.cntkModelPath, this.cntkEnvDirPath, 
            this.jsonTempDir, this.cntkInstallDir, this.verbose, cb);
        }
        catch(e) {
            return cb(e);
        }
    }
}

exports.EvalClient = EvalClient;