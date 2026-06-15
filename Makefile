# MemCell - Simple Makefile (works with g++ or MSVC cl.exe)
# Usage:
#   make            - build with g++
#   make CXX=cl     - build with MSVC
#   make test       - build and run tests
#   make clean      - remove build artifacts

CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra
INCLUDES = -Iinclude

SRCDIR = src
TESTDIR = tests
BUILDDIR = build

SOURCES = $(SRCDIR)/utils.cpp \
          $(SRCDIR)/cell.cpp \
          $(SRCDIR)/analyzer.cpp \
          $(SRCDIR)/chunker.cpp \
          $(SRCDIR)/dedup.cpp \
          $(SRCDIR)/pattern_codec.cpp \
          $(SRCDIR)/delta.cpp \
          $(SRCDIR)/lz_compress.cpp \
          $(SRCDIR)/entropy.cpp \
          $(SRCDIR)/shuffle.cpp \
          $(SRCDIR)/bitpack.cpp \
          $(SRCDIR)/file_format.cpp \
          $(SRCDIR)/memalgo.cpp

OBJECTS = $(patsubst $(SRCDIR)/%.cpp,$(BUILDDIR)/%.o,$(SOURCES))

.PHONY: all clean test

all: $(BUILDDIR) memalgo

$(BUILDDIR):
	mkdir -p $(BUILDDIR)

$(BUILDDIR)/%.o: $(SRCDIR)/%.cpp | $(BUILDDIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) -c $< -o $@

memalgo: $(OBJECTS) $(BUILDDIR)/main.o
	$(CXX) $(CXXFLAGS) $^ -o $@

$(BUILDDIR)/main.o: $(SRCDIR)/main.cpp | $(BUILDDIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) -c $< -o $@

memalgo_test: $(OBJECTS) $(BUILDDIR)/test_roundtrip.o
	$(CXX) $(CXXFLAGS) $^ -o $@

$(BUILDDIR)/test_roundtrip.o: $(TESTDIR)/test_roundtrip.cpp | $(BUILDDIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) -c $< -o $@

test: memalgo_test
	./memalgo_test

clean:
	rm -rf $(BUILDDIR) memalgo memalgo_test *.cell
