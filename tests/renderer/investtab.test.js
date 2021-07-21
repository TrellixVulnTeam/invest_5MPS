import React from 'react';
import { ipcRenderer } from 'electron';
import { fireEvent, render, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import userEvent from '@testing-library/user-event';

import InvestTab from '../../src/renderer/components/InvestTab';
import SetupTab from '../../src/renderer/components/SetupTab';
import {
  getSpec,
  saveToPython,
  writeParametersToFile,
  fetchValidation,
  fetchDatastackFromFile
} from '../../src/renderer/server_requests';
import InvestJob from '../../src/renderer/InvestJob';

jest.mock('../../src/renderer/server_requests');

const UI_CONFIG_PATH = '../../src/renderer/ui_config';

const DEFAULT_JOB = new InvestJob({
  modelRunName: 'carbon',
  modelHumanName: 'Carbon Model',
});
function renderInvestTab(job = DEFAULT_JOB) {
  const { ...utils } = render(
    <InvestTab
      job={job}
      jobID="carbon456asdf"
      investSettings={{ nWorkers: '-1', loggingLevel: 'INFO' }}
      saveJob={() => {}}
      updateJobProperty={() => {}}
    />
  );
  return utils;
}

// describe('Renders with data from a recent run', () => {
//   const job = new InvestJob({
//     modelRunName: 'carbon',
//     modelHumanName: 'Carbon Model',
//     status: 'error',
//     argsValues: {},
//     logfile: 'foo.txt',
//     finalTraceback: 'ValueError:',
//   });
// });

describe('Save InVEST Model Setup Buttons', () => {
  const spec = {
    module: 'natcap.invest.foo',
    model_name: 'FooModel',
    args: {
      workspace: {
        name: 'Workspace',
        type: 'directory',
        about: 'this is a workspace',
      },
      port: {
        name: 'Port',
        type: 'number',
      },
    },
  };

  // args expected to be in the saved JSON / Python dictionary
  const expectedArgKeys = ['workspace', 'n_workers'];

  beforeAll(() => {
    getSpec.mockResolvedValue(spec);
    fetchValidation.mockResolvedValue([]);
    // mock out the whole UI config module
    // brackets around spec.model_name turns it into a valid literal key
    const mockUISpec = {[spec.model_name]: {order: [Object.keys(spec.args)]}}
    jest.mock(UI_CONFIG_PATH, () => mockUISpec);
  });

  afterAll(() => {
    // the API for removing mocks is confusing (see https://github.com/facebook/jest/issues/7136)
    // not sure why, but resetModules is needed to unmock the ui_config
    jest.resetModules();
    jest.resetAllMocks();
    // Careful with reset because "resetting a spy results
    // in a function with no return value". I had been using spies to observe
    // function calls, but not to mock return values. Spies used for that
    // purpose should be 'restored' not 'reset'. Do that inside the test as-needed.
  });

  test('SaveParametersButton: requests endpoint with correct payload', async () => {
    // mock the server call, instead just returning
    // the payload. At least we can assert the payload is what
    // the flask endpoint needs to build the json file.
    writeParametersToFile.mockImplementation(
      (payload) => payload
    );
    const mockDialogData = { filePath: 'foo.json' };
    ipcRenderer.invoke.mockResolvedValueOnce(mockDialogData);

    const { findByText } = renderInvestTab();
    const saveButton = await findByText('Save to JSON');
    fireEvent.click(saveButton);

    await waitFor(() => {
      const results = writeParametersToFile.mock.results[0].value;
      expect(Object.keys(results)).toEqual(expect.arrayContaining(
        ['parameterSetPath', 'moduleName', 'relativePaths', 'args']
      ));
      Object.keys(results).forEach((key) => {
        expect(results[key]).not.toBeUndefined();
      });
      const args = JSON.parse(results.args);
      const argKeys = Object.keys(args);
      expect(argKeys).toEqual(expect.arrayContaining(expectedArgKeys));
      argKeys.forEach((key) => {
        expect(typeof args[key]).toBe('string');
      });
      expect(writeParametersToFile).toHaveBeenCalledTimes(1);
    });
  });

  test('SavePythonButton: requests endpoint with correct payload', async () => {
    // mock the server call, instead just returning
    // the payload. At least we can assert the payload is what
    // the flask endpoint needs to build the python script.
    saveToPython.mockImplementation(
      (payload) => payload
    );
    const mockDialogData = { filePath: 'foo.py' };
    ipcRenderer.invoke.mockResolvedValue(mockDialogData);

    const { findByText } = renderInvestTab();

    const saveButton = await findByText('Save to Python script');
    fireEvent.click(saveButton);

    await waitFor(() => {
      const results = saveToPython.mock.results[0].value;
      expect(Object.keys(results)).toEqual(expect.arrayContaining(
        ['filepath', 'modelname', 'pyname', 'args']
      ));
      Object.keys(results).forEach((key) => {
        expect(results[key]).not.toBeUndefined();
      });
      const args = JSON.parse(results.args);
      const argKeys = Object.keys(args);
      expect(argKeys).toEqual(expect.arrayContaining(expectedArgKeys));
      argKeys.forEach((key) => {
        expect(typeof args[key]).toBe('string');
      });
      expect(saveToPython).toHaveBeenCalledTimes(1);
    });
  });

  test('Load Parameters Button: loads parameters', async () => {
    const mockDatastack = {
      module_name: spec.module,
      args: {
        workspace: 'myworkspace',
        port: '9999',
      },
    };
    fetchDatastackFromFile.mockResolvedValue(mockDatastack);
    const mockDialogData = {
      filePaths: ['foo.json']
    };
    ipcRenderer.invoke.mockResolvedValue(mockDialogData);

    const { findByText, findByLabelText, queryByText } = renderInvestTab();

    const loadButton = await findByText('Load parameters from file');
    // test the tooltip before we click
    userEvent.hover(loadButton);
    const hoverText = 'Browse to a datastack (.json) or invest logfile (.txt)';
    expect(await findByText(hoverText)).toBeInTheDocument();
    userEvent.unhover(loadButton);
    await waitFor(() => {
      expect(queryByText(hoverText)).toBeNull();
    });
    fireEvent.click(loadButton);

    const input1 = await findByLabelText(spec.args.workspace.name);
    expect(input1).toHaveValue(mockDatastack.args.workspace);
    const input2 = await findByLabelText(spec.args.port.name);
    expect(input2).toHaveValue(mockDatastack.args.port);
  });

  test('SaveParametersButton: Dialog callback does nothing when canceled', async () => {
    // this resembles the callback data if the dialog is canceled instead of
    // a save file selected.
    const mockDialogData = {
      filePath: ''
    };
    ipcRenderer.invoke.mockResolvedValue(mockDialogData);
    // Spy on this method so we can assert it was never called.
    // Don't forget to restore! Otherwise a 'resetAllMocks'
    // can silently turn this spy into a function that returns nothing.
    const spy = jest.spyOn(SetupTab.prototype, 'saveJsonFile');

    const { findByText } = renderInvestTab();

    const saveButton = await findByText('Save to JSON');
    fireEvent.click(saveButton);

    // These are the calls that would have triggered if a file was selected
    expect(spy).toHaveBeenCalledTimes(0);
    spy.mockRestore(); // restores to unmocked implementation
  });

  test('SavePythonButton: Dialog callback does nothing when canceled', async () => {
    // this resembles the callback data if the dialog is canceled instead of 
    // a save file selected.
    const mockDialogData = {
      filePath: ''
    };
    ipcRenderer.invoke.mockResolvedValue(mockDialogData);
    // Spy on this method so we can assert it was never called.
    // Don't forget to restore! Otherwise the beforeEach will 'resetAllMocks'
    // will silently turn this spy into a function that returns nothing.
    const spy = jest.spyOn(SetupTab.prototype, 'savePythonScript');

    const { findByText } = renderInvestTab();

    const saveButton = await findByText('Save to Python script');
    fireEvent.click(saveButton);

    // These are the calls that would have triggered if a file was selected
    expect(spy).toHaveBeenCalledTimes(0);
    spy.mockRestore(); // restores to unmocked implementation
  });

  test('Load Parameters Button: does nothing when canceled', async () => {
    // this resembles the callback data if the dialog is canceled instead of 
    // a save file selected.
    const mockDialogData = {
      filePaths: ['']
    };
    ipcRenderer.invoke.mockResolvedValue(mockDialogData);
    // Spy on this method so we can assert it was never called.
    // Don't forget to restore! Otherwise the beforeEach will 'resetAllMocks'
    // will silently turn this spy into a function that returns nothing.
    const spy = jest.spyOn(SetupTab.prototype, 'loadParametersFromFile');

    const { findByText } = renderInvestTab();

    const loadButton = await findByText('Load parameters from file');
    fireEvent.click(loadButton);

    // These are the calls that would have triggered if a file was selected
    expect(spy).toHaveBeenCalledTimes(0);
    spy.mockRestore(); // restores to unmocked implementation
  });
});

describe('InVEST Run Button', () => {
  const spec = {
    module: 'natcap.invest.bar',
    model_name: 'BarModel',
    args: {
      a: {
        name: 'abar',
        type: 'freestyle_string',
      },
      b: {
        name: 'bbar',
        type: 'number',
      },
      c: {
        name: 'cbar',
        type: 'csv',
      },
    },
  };

  beforeAll(() => {
    getSpec.mockResolvedValue(spec);
    // mock out the whole UI config module
    // brackets around spec.model_name turns it into a valid literal key
    let mockUISpec = {[spec.model_name]: {order: [Object.keys(spec.args)]}}
    jest.mock(UI_CONFIG_PATH, () => mockUISpec);
  });

  afterAll(() => {
    jest.resetModules();
    jest.resetAllMocks();
  });

  test('Changing inputs trigger validation & enable/disable Run', async () => {
    let invalidFeedback = 'is a required key';
    fetchValidation.mockResolvedValue([[['a', 'b'], invalidFeedback]]);

    const {
      findByLabelText,
      findByRole,
    } = renderInvestTab();

    const runButton = await findByRole('button', { name: /Run/ });
    expect(runButton).toBeDisabled();

    const a = await findByLabelText(RegExp(`${spec.args.a.name}`));
    const b = await findByLabelText(RegExp(`${spec.args.b.name}`));

    expect(a).toHaveClass('is-invalid');
    expect(b).toHaveClass('is-invalid');

    // These new values will be valid - Run should enable
    fetchValidation.mockResolvedValue([]);
    fireEvent.change(a, { target: { value: 'foo' } });
    fireEvent.change(b, { target: { value: 1 } });
    await waitFor(() => {
      expect(runButton).toBeEnabled();
    });

    // This new value will be invalid - Run should disable again
    invalidFeedback = 'must be a number';
    fetchValidation.mockResolvedValue([[['b'], invalidFeedback]]);
    fireEvent.change(b, { target: { value: 'one' } });
    await waitFor(() => {
      expect(runButton).toBeDisabled();
    });
  });
});
