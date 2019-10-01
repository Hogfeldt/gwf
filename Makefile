.PHONY: \
	help \
	init \
	test \
	lint \
	coverage \
	docs \
	clean \
	package-pypi \
	publish-pypi \
	package-conda \
	publish-conda

help:
	@echo "Usage:"
	@echo "    make help              show this message"
	@echo "    make init              create and setup development environment"
	@echo "    make test              run unit tests"
	@echo "    make lint              run lint checks"
	@echo "    make test              build docs"
	@echo "    make clean             remove temporary files"
	@echo "    make package           build source distribution and wheel"
	@echo "    make publish           publish distributions to pypi"
	@echo "    make package-conda     build source distribution and wheel"
	@echo "    make publish-conda     publish distributions to pypi"

init:
	pip install -r requirements.txt
	pip install -e . --no-deps

test:
	coverage run --source gwf -m pytest --doctest-modules --ignore=examples/

lint:
	flake8 src/gwf

coverage:
	coverage report

docs:
	$(MAKE) -C docs html

clean:
	find . -name "*.egg-info" -type d -exec rm -rf {} ';'
	find . -name ".gwf" -type d -exec rm -rf {} ';'
	rm -rf docs/_build .gwfconf.json build/ dist/ .gwf .pytest_cache .egg conda-bld

# PyPI

package: clean
	python setup.py sdist bdist_wheel

publish:
	twine upload dist/*

# Conda

package-conda: clean
	conda build --output-folder conda-bld/ conda/

publish-conda:
	anaconda -t "${ANACONDA_TOKEN}" upload --force --no-progress --user gwforg conda-bld/*/*.tar.bz2
